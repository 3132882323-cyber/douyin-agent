const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const readbackPolicy = require("./execution-readback-policy.js");

const background = fs.readFileSync(path.join(__dirname, "background.js"), "utf8");

const tokenStart = background.indexOf("function createExecutionReadbackToken");
const tokenEnd = background.indexOf("\nfunction isScanCancelled", tokenStart);
assert.ok(tokenStart >= 0 && tokenEnd > tokenStart, "the execution readback token generator should be extractable");
const tokenBlock = background.slice(tokenStart, tokenEnd);
assert.match(tokenBlock, /getRandomValues/);
assert.match(tokenBlock, /Uint8Array\(32\)/, "a readback token must contain 256 bits of browser-generated entropy");

const tokenSandbox = { crypto: require("node:crypto").webcrypto, Uint8Array };
tokenSandbox.globalThis = tokenSandbox;
vm.runInNewContext(`${tokenBlock}\nglobalThis.__createExecutionReadbackToken = createExecutionReadbackToken;`, tokenSandbox);
const generatedTokens = new Set(Array.from({ length: 32 }, () => tokenSandbox.__createExecutionReadbackToken()));
assert.equal(generatedTokens.size, 32, "independent readback attempts must not reuse a token");
for (const token of generatedTokens) assert.match(token, /^[a-f0-9]{64}$/);

const verifyStart = background.indexOf("async function verifyExecutionAction");
const verifyEnd = background.indexOf("\nasync function readExecutionDocumentWitness", verifyStart);
assert.ok(verifyStart >= 0 && verifyEnd > verifyStart, "execution verification should be extractable");
const verifyBlock = background.slice(verifyStart, verifyEnd);

const storeStart = background.indexOf("async function storeAndPush");
const storeEnd = background.indexOf("\nasync function updateStatus", storeStart);
const conflictHelperStart = background.indexOf("function snapshotHasIdentityConflict");
const conflictHelperEnd = background.indexOf("\nfunction requireDoudianSnapshotIdentity", conflictHelperStart);
assert.ok(storeStart >= 0 && storeEnd > storeStart, "page-data push should be extractable");
assert.ok(conflictHelperStart >= 0 && conflictHelperEnd > conflictHelperStart, "identity conflict policy should be extractable");
const conflictHelperBlock = background.slice(conflictHelperStart, conflictHelperEnd);
const storeBlock = background.slice(storeStart, storeEnd);

const attemptStart = background.indexOf("async function runExecutionReadbackAttempt");
const attemptEnd = background.indexOf("\nasync function runExecutionReadbackJob", attemptStart);
assert.ok(attemptStart >= 0 && attemptEnd > attemptStart, "execution readback attempt block should exist");
const attemptBlock = background.slice(attemptStart, attemptEnd);

const infrastructureStart = background.indexOf("function isReadbackInfrastructureFailure");
const infrastructureEnd = background.indexOf("\nasync function verifyExecutionAction", infrastructureStart);
assert.ok(infrastructureStart >= 0 && infrastructureEnd > infrastructureStart, "readback infrastructure classifier should be extractable");
const infrastructureBlock = background.slice(infrastructureStart, infrastructureEnd);
const infrastructureSandbox = {};
vm.runInNewContext(`${infrastructureBlock}\nglobalThis.__isInfrastructure = isReadbackInfrastructureFailure;`, infrastructureSandbox);
assert.equal(infrastructureSandbox.__isInfrastructure({ status: 503 }), true);
assert.equal(infrastructureSandbox.__isInfrastructure({ status: 429 }), true);
assert.equal(infrastructureSandbox.__isInfrastructure({ code: "COLLECTION_CONTEXT_CHANGED" }), true);
assert.equal(infrastructureSandbox.__isInfrastructure({ code: "READBACK_COLLECTION_RESPONSE_INVALID" }), true);
assert.equal(infrastructureSandbox.__isInfrastructure({ code: "READBACK_VERIFY_RESPONSE_INVALID", retryable: true }), true);
assert.equal(infrastructureSandbox.__isInfrastructure({ code: "SNAPSHOT_ACCEPTANCE_UNCONFIRMED" }), true);
assert.equal(infrastructureSandbox.__isInfrastructure({ code: "TARGETED_EXECUTION_ACCEPTANCE_UNCONFIRMED" }), true);
assert.equal(infrastructureSandbox.__isInfrastructure({ status: 409, retryable: true, code: "REQUEST_ALREADY_IN_PROGRESS" }), true);
assert.equal(infrastructureSandbox.__isInfrastructure({ status: 409, retryable: false, code: "IMMUTABLE_TOKEN_CONFLICT" }), false);
assert.equal(infrastructureSandbox.__isInfrastructure({ status: 400, code: "SCHEMA_REJECTED" }), false);

assert.match(attemptBlock, /let job = policy\.normalizeJob\(jobValue\)/);
assert.match(attemptBlock, /if \(job\.status !== "pending"\) return job/);
assert.match(attemptBlock, /deferPush:\s*true/);
assert.match(attemptBlock, /expectedStoreKey:\s*job\.store_key/);
assert.match(attemptBlock, /expectedAccountKey:\s*job\.account_key/);
assert.match(attemptBlock, /readbackTabUnavailable/);
assert.match(attemptBlock, /policy\.deferAttempt\(job/);
const scopedStoreIndex = attemptBlock.indexOf("const bridgeResult = await storeAndPush");
const postPushVerifyIndex = attemptBlock.indexOf("verification = await verifyExecutionAction", scopedStoreIndex);
assert.ok(
  scopedStoreIndex >= 0 && postPushVerifyIndex > scopedStoreIndex,
  "a newly collected candidate must pass scoped storage validation before action verification",
);
const scopedPushBlock = attemptBlock.slice(
  attemptBlock.indexOf("const bridgeResult = await storeAndPush"),
  attemptBlock.indexOf("if (!bridgeResult?.ok)"),
);
assert.doesNotMatch(scopedPushBlock, /runId|pageId|attemptId/, "execution readback must not impersonate a full-scan run");

const executeStart = background.indexOf("async function runAuthorizedExecution");
const executeEnd = background.indexOf("\nasync function storeAndPush", executeStart);
assert.ok(executeStart >= 0 && executeEnd > executeStart, "authorized execution block should exist");
const executeBlock = background.slice(executeStart, executeEnd);
assert.match(executeBlock, /store_key:\s*request\.promotion_context\?\.account_scope\?\.store_id/);
assert.match(executeBlock, /account_key:\s*request\.account_key/);
assert.match(executeBlock, /plan_id:\s*request\.plan_id/);

assert.match(background, /const executionReadbackRunPromises = new Map\(\)/);
assert.match(background, /executionReadbackRunPromises\.get\(actionId\)/);
assert.match(background, /executionReadbackRunPromises\.delete\(actionId\)/);

const manualStart = background.indexOf("async function runManualExecutionReadback");
const manualEnd = background.indexOf("\nlet executionReadbackRecoveryPromise", manualStart);
assert.ok(manualStart >= 0 && manualEnd > manualStart, "manual readback entry should exist");
const manualBlock = background.slice(manualStart, manualEnd);
assert.match(manualBlock, /reopenManualAttempt\(storedJob/);
assert.match(manualBlock, /allowTerminalReopen:\s*true/);
assert.match(manualBlock, /singleAttempt:\s*true/);

const matcherStart = background.indexOf("function qianchuanReadbackScopeMatches");
const matcherEnd = background.indexOf("\nfunction readbackTabUnavailable", matcherStart);
assert.ok(matcherStart >= 0 && matcherEnd > matcherStart, "scoped readback tab matcher should exist");
const matcherSandbox = {};
vm.runInNewContext(background.slice(matcherStart, matcherEnd), matcherSandbox);
const job = { store_key: "store-a", account_key: "account-a" };
assert.equal(matcherSandbox.qianchuanReadbackScopeMatches({
  store_key: "STORE-A",
  account_key: "ACCOUNT-A",
}, job), true);
assert.equal(matcherSandbox.qianchuanReadbackScopeMatches({
  store_key: "store-b",
  account_key: "account-a",
}, job), false);
assert.equal(matcherSandbox.qianchuanReadbackScopeMatches({
  account: { key: "account-a" },
}, job), false, "account-only metadata must not select a fallback tab");

const resolverStart = background.indexOf("async function resolveExecutionReadbackTab");
const resolverEnd = background.indexOf("\nasync function verifyExecutionAction", resolverStart);
const resolverBlock = background.slice(resolverStart, resolverEnd);
assert.doesNotMatch(resolverBlock, /selectQianchuanSyncTab/);
assert.match(resolverBlock, /qianchuanReadbackScopeMatches/);
assert.match(resolverBlock, /throw readbackTabUnavailable/);

const witnessStart = background.indexOf("async function readExecutionDocumentWitness");
const witnessEnd = background.indexOf("\nasync function runExecutionReadbackAttempt", witnessStart);
assert.ok(witnessStart >= 0 && witnessEnd > witnessStart, "independent document reload helper should exist");
const tabStates = [
  { id: 7, status: "complete", url: "https://qianchuan.jinritemai.com/campaigns" },
  { id: 7, status: "loading", url: "https://qianchuan.jinritemai.com/campaigns" },
  { id: 7, status: "complete", url: "https://qianchuan.jinritemai.com/campaigns" },
  { id: 7, status: "complete", url: "https://qianchuan.jinritemai.com/campaigns" },
  { id: 7, status: "loading", url: "https://qianchuan.jinritemai.com/campaigns" },
  { id: 7, status: "complete", url: "https://qianchuan.jinritemai.com/campaigns" },
];
const futureNavigation = Date.now() + 10_000;
const witnesses = [
  { ok: true, witness: { document_instance_id: "document-old-0001", navigation_started_at_ms: 500 } },
  { ok: true, witness: { document_instance_id: "document-old-0001", navigation_started_at_ms: 500 } },
  { ok: true, witness: { document_instance_id: "document-new-0002", navigation_started_at_ms: futureNavigation } },
  { ok: true, witness: { document_instance_id: "document-new-0002", navigation_started_at_ms: futureNavigation } },
  { ok: true, witness: { document_instance_id: "document-new-0002", navigation_started_at_ms: futureNavigation } },
  { ok: true, witness: { document_instance_id: "document-next-0003", navigation_started_at_ms: futureNavigation + 1_000 } },
];
let getCount = 0;
let witnessCount = 0;
const witnessSandbox = {
  chrome: {
    tabs: {
      reload: async () => undefined,
      get: async () => tabStates[Math.min(getCount++, tabStates.length - 1)],
      sendMessage: async () => witnesses[Math.min(witnessCount++, witnesses.length - 1)],
    },
  },
  DianAgentScanPolicy: { isQianchuanUrl: () => true },
  sleep: async () => undefined,
};
vm.runInNewContext(background.slice(witnessStart, witnessEnd), witnessSandbox);

const changedScopeHash = "e".repeat(64);
const originalScopeHash = "f".repeat(64);
let changedScopeWitnessIndex = 0;
const changedScopeWitnesses = [
  {
    ok: true,
    witness: {
      document_instance_id: "document-scope-old",
      navigation_started_at_ms: 500,
      identity: { status: "resolved_by_bridge", claims: [{ kind: "qianchuan_advertiser_id", raw_id: "account-a" }] },
      promotion_mode: "standard",
      page_type: "campaigns",
    },
  },
  {
    ok: true,
    witness: {
      document_instance_id: "document-scope-new",
      navigation_started_at_ms: Date.now() + 10_000,
      identity: { status: "resolved_by_bridge", claims: [{ kind: "qianchuan_advertiser_id", raw_id: "account-b" }] },
      promotion_mode: "chengfang",
      page_type: "qianchuan_live",
    },
  },
];
const changedScopeSandbox = {
  chrome: {
    tabs: {
      reload: async () => undefined,
      get: async () => ({ id: 9, status: "complete", url: "https://qianchuan.jinritemai.com/campaigns" }),
      sendMessage: async () => changedScopeWitnesses[Math.min(changedScopeWitnessIndex++, changedScopeWitnesses.length - 1)],
    },
  },
  DianAgentScanPolicy: { isQianchuanUrl: () => true },
  executionWitnessScopeHash: async (witness) => witness.promotion_mode === "standard" ? originalScopeHash : changedScopeHash,
  sleep: async () => undefined,
};
vm.runInNewContext(background.slice(witnessStart, witnessEnd), changedScopeSandbox);

const candidateAttempts = [];
const collectedOptions = [];
const pushedOptions = [];
const persistedAttempts = [];
const verificationCalls = [];
let verifiedCount = 0;
const attemptSandbox = {
  crypto: require("node:crypto").webcrypto,
  Uint8Array,
  DianExecutionReadbackPolicy: readbackPolicy,
  resolveExecutionReadbackTab: async (_job, excludedTabIds) => {
    candidateAttempts.push([...excludedTabIds]);
    return excludedTabIds.includes(11) ? { id: 22 } : { id: 11 };
  },
  reloadExecutionReadbackTab: async (tab) => ({
    ...tab,
    readback_document_witness: {
      document_instance_id: `document-readback-${tab.id}`,
      navigation_started_at_ms: Date.now() + tab.id,
    },
  }),
  collectFromTab: async (_source, tab, reason, options) => {
    assert.match(reason, /^(?:full-scan-list-)?post-execution-readback-\d+$/);
    assert.equal(options.deferPush, true);
    collectedOptions.push(options);
    return {
      ok: true,
      snapshot: {
        candidate_tab_id: tab.id,
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
  storeAndPush: async (_source, snapshot, options) => {
    pushedOptions.push({ snapshot, options });
    if (snapshot.candidate_tab_id === 11) {
      return { ok: false, error: "wrong account", error_code: "READBACK_SCOPE_REJECTED" };
    }
    return { ok: true };
  },
  verifyExecutionAction: async (actionId, readbackToken) => {
    verifiedCount += 1;
    verificationCalls.push({ actionId, readbackToken });
    return { state: "verified", verified: true, readback_token: readbackToken };
  },
  isReadbackScopeFailure: (error) => ["READBACK_SCOPE_REJECTED", "READBACK_SCOPE_TAB_UNAVAILABLE"].includes(error?.code),
  isReadbackInfrastructureFailure: () => false,
  isReadbackPairingFailure: () => false,
  readbackTabUnavailable: (message) => Object.assign(new Error(message), { code: "READBACK_SCOPE_TAB_UNAVAILABLE" }),
  persistExecutionReadbackJob: async (value) => {
    persistedAttempts.push(value);
    return value;
  },
};
attemptSandbox.globalThis = attemptSandbox;
vm.runInNewContext(`${tokenBlock}\n${attemptBlock}`, attemptSandbox);

const scopeRejectPersisted = [];
const scopeRejectAttemptSandbox = {
  crypto: require("node:crypto").webcrypto,
  Uint8Array,
  DianExecutionReadbackPolicy: readbackPolicy,
  resolveExecutionReadbackTab: async (_job, excludedTabIds) => {
    if (excludedTabIds.includes(33)) {
      const error = new Error("no unchanged scope remains");
      error.code = "READBACK_SCOPE_TAB_UNAVAILABLE";
      throw error;
    }
    return { id: 33 };
  },
  reloadExecutionReadbackTab: async () => {
    const error = new Error("identity/mode/page changed after hard reload");
    error.code = "READBACK_SCOPE_REJECTED";
    throw error;
  },
  collectFromTab: async () => { throw new Error("scope rejection must stop before collection"); },
  storeAndPush: async () => { throw new Error("scope rejection must stop before push"); },
  verifyExecutionAction: async () => { throw new Error("scope rejection must stop before verification"); },
  isReadbackScopeFailure: (error) => ["READBACK_SCOPE_REJECTED", "READBACK_SCOPE_TAB_UNAVAILABLE"].includes(error?.code),
  isReadbackInfrastructureFailure: () => false,
  isReadbackPairingFailure: () => false,
  readbackTabUnavailable: (message) => Object.assign(new Error(message), { code: "READBACK_SCOPE_TAB_UNAVAILABLE" }),
  persistExecutionReadbackJob: async (value) => {
    scopeRejectPersisted.push(value);
    return value;
  },
};
scopeRejectAttemptSandbox.globalThis = scopeRejectAttemptSandbox;
vm.runInNewContext(`${tokenBlock}\n${attemptBlock}`, scopeRejectAttemptSandbox);

const committedToken = "7f".repeat(32);
const committedCalls = { persisted: 0, verified: 0 };
const committedSandbox = {
  crypto: require("node:crypto").webcrypto,
  Uint8Array,
  DianExecutionReadbackPolicy: readbackPolicy,
  resolveExecutionReadbackTab: async () => { throw new Error("a committed verification must not recollect the page"); },
  reloadExecutionReadbackTab: async () => { throw new Error("a committed verification must not reload the page"); },
  collectFromTab: async () => { throw new Error("a committed verification must not recollect the page"); },
  storeAndPush: async () => { throw new Error("a committed verification must not repush a snapshot"); },
  verifyExecutionAction: async (_actionId, token) => {
    committedCalls.verified += 1;
    assert.equal(token, committedToken);
    return {
      state: "verified",
      verified: true,
      readback_token: token,
      snapshot_ref: `execution_readbacks/${"7".repeat(24)}/${token}`,
      readback: { plan_id: "plan-committed" },
    };
  },
  isReadbackScopeFailure: () => false,
  isReadbackInfrastructureFailure: () => false,
  readbackTabUnavailable: (message) => Object.assign(new Error(message), { code: "READBACK_SCOPE_TAB_UNAVAILABLE" }),
  persistExecutionReadbackJob: async (value) => {
    committedCalls.persisted += 1;
    return readbackPolicy.normalizeJob(value);
  },
};
committedSandbox.globalThis = committedSandbox;
vm.runInNewContext(`${tokenBlock}\n${attemptBlock}`, committedSandbox);

const retryableConflictSandbox = {
  ...committedSandbox,
  verifyExecutionAction: async () => {
    const error = new Error("REQUEST_ALREADY_IN_PROGRESS");
    error.code = "REQUEST_ALREADY_IN_PROGRESS";
    error.status = 409;
    error.retryable = true;
    throw error;
  },
  persistExecutionReadbackJob: async (value) => readbackPolicy.normalizeJob(value),
};
retryableConflictSandbox.globalThis = retryableConflictSandbox;
vm.runInNewContext(`${tokenBlock}\n${infrastructureBlock}\n${attemptBlock}`, retryableConflictSandbox);

const invalidBodyAttemptSandbox = {
  ...committedSandbox,
  verifyExecutionAction: async () => {
    const error = new Error("truncated verification response");
    error.code = "READBACK_VERIFY_RESPONSE_INVALID";
    error.status = 200;
    error.retryable = true;
    throw error;
  },
  persistExecutionReadbackJob: async (value) => readbackPolicy.normalizeJob(value),
};
invalidBodyAttemptSandbox.globalThis = invalidBodyAttemptSandbox;
vm.runInNewContext(`${tokenBlock}\n${infrastructureBlock}\n${attemptBlock}`, invalidBodyAttemptSandbox);

const pairingFailureSandbox = {
  ...committedSandbox,
  verifyExecutionAction: async () => {
    const error = new Error("extension pairing is not authorized");
    error.code = "extension_pairing_not_authorized";
    error.status = 403;
    throw error;
  },
  persistExecutionReadbackJob: async (value) => readbackPolicy.normalizeJob(value),
};
pairingFailureSandbox.globalThis = pairingFailureSandbox;
vm.runInNewContext(`${tokenBlock}\n${infrastructureBlock}\n${attemptBlock}`, pairingFailureSandbox);

const missingEvidenceToken = "3e".repeat(32);
const missingEvidenceCalls = { verified: 0, collected: 0, pushed: 0 };
const missingEvidenceSandbox = {
  crypto: require("node:crypto").webcrypto,
  Uint8Array,
  DianExecutionReadbackPolicy: readbackPolicy,
  resolveExecutionReadbackTab: async () => ({ id: 66 }),
  reloadExecutionReadbackTab: async (tab) => ({
    ...tab,
    readback_document_witness: { document_instance_id: "document-missing-then-new", navigation_started_at_ms: Date.now() + 1 },
  }),
  collectFromTab: async () => {
    missingEvidenceCalls.collected += 1;
    return {
      ok: true,
      snapshot: { quality: { targeted: true, scope: "target_plan", coverage_complete: false, collection_complete: false, target_plan_found: true } },
    };
  },
  storeAndPush: async () => { missingEvidenceCalls.pushed += 1; return { ok: true }; },
  verifyExecutionAction: async (_actionId, token) => {
    missingEvidenceCalls.verified += 1;
    if (missingEvidenceCalls.verified === 1) {
      return { state: "executing", verified: false, readback: null, snapshot_ref: "", readback_token: token };
    }
    return {
      state: "verified",
      verified: true,
      readback_token: token,
      snapshot_ref: `execution_readbacks/${"3".repeat(24)}/${token}`,
      readback: { plan_id: "plan-missing" },
    };
  },
  isReadbackScopeFailure: () => false,
  isReadbackInfrastructureFailure: () => false,
  readbackTabUnavailable: (message) => Object.assign(new Error(message), { code: "READBACK_SCOPE_TAB_UNAVAILABLE" }),
  persistExecutionReadbackJob: async (value) => readbackPolicy.normalizeJob(value),
};
missingEvidenceSandbox.globalThis = missingEvidenceSandbox;
vm.runInNewContext(`${tokenBlock}\n${attemptBlock}`, missingEvidenceSandbox);

const unavailableCalls = { persisted: [] };
const unavailableSandbox = {
  crypto: require("node:crypto").webcrypto,
  Uint8Array,
  DianExecutionReadbackPolicy: readbackPolicy,
  resolveExecutionReadbackTab: async () => ({ id: 55 }),
  reloadExecutionReadbackTab: async (tab) => ({
    ...tab,
    readback_document_witness: { document_instance_id: "document-http-503", navigation_started_at_ms: Date.now() + 1 },
  }),
  collectFromTab: async () => ({
    ok: true,
    snapshot: { quality: { targeted: true, scope: "target_plan", coverage_complete: false, collection_complete: false, target_plan_found: true } },
  }),
  storeAndPush: async () => ({ ok: false, status: 503, bridge_reachable: true, error: "Agent unavailable", error_code: "AGENT_UNAVAILABLE" }),
  verifyExecutionAction: async () => { throw new Error("503 must stop before verification"); },
  isReadbackScopeFailure: () => false,
  readbackTabUnavailable: (message) => Object.assign(new Error(message), { code: "READBACK_SCOPE_TAB_UNAVAILABLE" }),
  persistExecutionReadbackJob: async (value) => {
    const normalized = readbackPolicy.normalizeJob(value);
    unavailableCalls.persisted.push(normalized);
    return normalized;
  },
};
unavailableSandbox.globalThis = unavailableSandbox;
vm.runInNewContext(`${tokenBlock}\n${infrastructureBlock}\n${attemptBlock}`, unavailableSandbox);

const contextChangedSandbox = {
  ...unavailableSandbox,
  collectFromTab: async () => ({
    ok: false,
    code: "COLLECTION_CONTEXT_CHANGED",
    error: "QIANCHUAN_CAPTURE_CONTEXT_CHANGED",
  }),
  storeAndPush: async () => { throw new Error("a changed SPA context must stop before push"); },
  verifyExecutionAction: async () => { throw new Error("a changed SPA context must stop before verification"); },
  persistExecutionReadbackJob: async (value) => readbackPolicy.normalizeJob(value),
};
contextChangedSandbox.globalThis = contextChangedSandbox;
vm.runInNewContext(`${tokenBlock}\n${infrastructureBlock}\n${attemptBlock}`, contextChangedSandbox);

function createInvalidCollectionCase(snapshot, tabId) {
  const calls = { collected: 0, pushed: 0, verified: 0, persisted: [] };
  const sandbox = {
    crypto: require("node:crypto").webcrypto,
    Uint8Array,
    DianExecutionReadbackPolicy: readbackPolicy,
    resolveExecutionReadbackTab: async () => ({ id: tabId }),
    reloadExecutionReadbackTab: async (tab) => ({
      ...tab,
      readback_document_witness: {
        document_instance_id: `document-invalid-collection-${tabId}`,
        navigation_started_at_ms: Date.now() + 1,
      },
    }),
    collectFromTab: async () => {
      calls.collected += 1;
      return { ok: true, snapshot };
    },
    storeAndPush: async () => {
      calls.pushed += 1;
      throw new Error("an invalid collection response must stop before snapshot storage");
    },
    verifyExecutionAction: async () => {
      calls.verified += 1;
      const error = new Error("the replay token has no committed snapshot yet");
      error.code = "EXECUTION_READBACK_SNAPSHOT_NOT_FOUND";
      error.status = 404;
      throw error;
    },
    isReadbackScopeFailure: () => false,
    readbackTabUnavailable: (message) => Object.assign(new Error(message), { code: "READBACK_SCOPE_TAB_UNAVAILABLE" }),
    persistExecutionReadbackJob: async (value) => {
      const normalized = readbackPolicy.normalizeJob(value);
      calls.persisted.push(normalized);
      return normalized;
    },
  };
  sandbox.globalThis = sandbox;
  vm.runInNewContext(`${tokenBlock}\n${infrastructureBlock}\n${attemptBlock}`, sandbox);
  return { sandbox, calls };
}

const invalidCollectionCases = [
  { name: "missing snapshot", snapshot: undefined, actionHex: "a", tabId: 71 },
  { name: "non-object snapshot", snapshot: "not-a-snapshot", actionHex: "b", tabId: 72 },
  { name: "array snapshot", snapshot: [], actionHex: "c", tabId: 73 },
].map((item) => ({ ...item, ...createInvalidCollectionCase(item.snapshot, item.tabId) }));

const manualCalls = { bridge: [], persisted: [], readbacks: [], forbidden: [] };
const reconstructedJob = {
  ...readbackPolicy.createJob({
    action_id: "9".repeat(24),
    store_key: "store-rebuilt",
    account_key: "account-rebuilt",
    plan_id: "plan-rebuilt",
    tab_id: 44,
    baseline_scope_hash: "a".repeat(64),
    baseline_document_instance_id: "document-rebuilt-old",
    baseline_navigation_started_at_ms: 1_000,
    baseline_promotion_mode: "standard",
    baseline_page_type: "campaigns",
  }, 1_000),
  baseline_scope_hash: "a".repeat(64),
  baseline_document_instance_id: "document-rebuilt-old",
  baseline_navigation_started_at_ms: 1_000,
  baseline_promotion_mode: "standard",
  baseline_page_type: "campaigns",
  write_enabled: true,
  execution_enabled: true,
  status: "uncertain",
  next_attempt_index: 4,
  next_attempt_at_ms: 0,
};
const manualSandbox = {
  BRIDGE_URL: "http://127.0.0.1:8765",
  executionReadbackRunPromises: new Map(),
  DianExecutionReadbackPolicy: readbackPolicy,
  loadExecutionReadbackJobs: async () => ({}),
  bridgeFetch: async (url, init = {}) => {
    manualCalls.bridge.push({ url, init });
    if (/preflight\/preview|preflight\/consume|actions\/execution\/result/.test(url)) {
      manualCalls.forbidden.push(url);
      throw new Error(`forbidden write endpoint: ${url}`);
    }
    return {
      ok: true,
      json: async () => ({ job: reconstructedJob }),
    };
  },
  persistExecutionReadbackJob: async (job, options) => {
    const normalized = readbackPolicy.normalizeJob(job);
    manualCalls.persisted.push({ job: normalized, options });
    return normalized;
  },
  runExecutionReadbackJob: async (job, options) => {
    manualCalls.readbacks.push({ job, options });
    return { ...job, status: "verified", last_verification: { verified: true, state: "verified" } };
  },
  chrome: {
    tabs: {
      sendMessage: async (_tabId, message) => {
        if (message?.type === "qianchuan-supervised-submit") throw new Error("manual reconstruction must never execute");
        return { ok: true };
      },
    },
  },
};
manualSandbox.globalThis = manualSandbox;
vm.runInNewContext(`${manualBlock}\nglobalThis.__runManualExecutionReadback = runManualExecutionReadback;`, manualSandbox);

let blockedManualSideEffects = 0;
const blockedManualSandbox = {
  ensureVersionReloadStateHydrated: async () => ({ hydrated: true, reload_pending: true }),
  assertVersionReloadAllowsNewWork: () => {
    const error = new Error("reload pending");
    error.code = "EXTENSION_RELOAD_PENDING";
    throw error;
  },
  loadExecutionReadbackJobs: async () => { blockedManualSideEffects += 1; return {}; },
  bridgeFetch: async () => { blockedManualSideEffects += 1; throw new Error("must not run"); },
  persistExecutionReadbackJob: async () => { blockedManualSideEffects += 1; throw new Error("must not run"); },
  executionReadbackRunPromises: new Map(),
};
blockedManualSandbox.globalThis = blockedManualSandbox;
vm.runInNewContext(
  `${manualBlock}\nglobalThis.__runManualExecutionReadback = runManualExecutionReadback;`,
  blockedManualSandbox,
);

const verifyRequests = [];
const verifySandbox = {
  BRIDGE_URL: "http://127.0.0.1:8765",
  bridgeFetch: async (url, init) => {
    verifyRequests.push({ url, init });
    const request = JSON.parse(init.body);
    return {
      ok: true,
      json: async () => ({
        verification: {
          action_id: "f".repeat(24),
          state: "verified",
          verified: true,
          readback_token: request.readback_token,
          snapshot_ref: `execution_readbacks/${"f".repeat(24)}/${request.readback_token}`,
          readback: { plan_id: "plan-verified" },
        },
      }),
    };
  },
};
vm.runInNewContext(`${verifyBlock}\nglobalThis.__verifyExecutionAction = verifyExecutionAction;`, verifySandbox);

const invalidVerifySandbox = {
  BRIDGE_URL: "http://127.0.0.1:8765",
  bridgeFetch: async () => ({
    ok: true,
    status: 200,
    json: async () => { throw new SyntaxError("truncated JSON"); },
  }),
};
vm.runInNewContext(`${verifyBlock}\nglobalThis.__verifyExecutionAction = verifyExecutionAction;`, invalidVerifySandbox);

const pushRequests = [];
const storeSandbox = {
  BRIDGE_URL: "http://127.0.0.1:8765",
  SOURCE_PATTERNS: { qianchuan: ["https://qianchuan.jinritemai.com/*"] },
  fetchWithTimeout: async (url, init) => {
    pushRequests.push({ url, init });
    return {
      ok: true,
      status: 200,
      json: async () => ({
        ok: true,
        accepted: true,
        accepted_for_current_data: false,
        targeted_execution_snapshot: true,
      }),
    };
  },
  mutateLocalStorage: async (_keys, mutator) => mutator({ catalog: {} }),
  structuredClone,
  updateStatus: async () => undefined,
  scanErrorCode: () => "TEST_ERROR",
};
vm.runInNewContext(`${conflictHelperBlock}\n${storeBlock}\nglobalThis.__storeAndPush = storeAndPush;`, storeSandbox);

(async () => {
  await assert.rejects(
    blockedManualSandbox.__runManualExecutionReadback("8".repeat(24)),
    (error) => error.code === "EXTENSION_RELOAD_PENDING",
  );
  assert.equal(blockedManualSideEffects, 0, "manual readback must pass reload admission before bridge recovery or persistence");

  const result = await witnessSandbox.reloadExecutionReadbackTab(
    { id: 7 },
    {
      baseline_document_instance_id: "document-old-0001",
      baseline_navigation_started_at_ms: 500,
      started_at_ms: 1_000,
    },
  );
  assert.equal(result.readback_document_witness.document_instance_id, "document-new-0002");
  const second = await witnessSandbox.reloadExecutionReadbackTab(
    { id: 7 },
    {
      baseline_document_instance_id: "document-old-0001",
      started_at_ms: 1_000,
    },
  );
  assert.equal(second.readback_document_witness.document_instance_id, "document-next-0003");
  assert.equal(getCount, 6, "each reload must wait past its own old complete document");
  assert.equal(witnessCount, 6, "each attempt probes its current document before and after reload");

  assert.equal(
    await changedScopeSandbox.executionWitnessScopeHash(changedScopeWitnesses[0].witness),
    originalScopeHash,
    "the pre-reload identity/mode/page witness must match the authorized scope hash",
  );
  await assert.rejects(
    changedScopeSandbox.reloadExecutionReadbackTab(
      { id: 9 },
      { started_at_ms: 1_000, baseline_scope_hash: originalScopeHash },
    ),
    (error) => error.code === "READBACK_SCOPE_REJECTED",
  );
  assert.equal(changedScopeWitnessIndex, 3, "a stable post-reload scope mismatch must be observed twice before rejection");

  const rejectedScopeJob = {
    ...readbackPolicy.createJob({
      action_id: "8".repeat(24),
      store_key: "store-a",
      account_key: "account-a",
      plan_id: "plan-a",
      baseline_scope_hash: originalScopeHash,
      baseline_promotion_mode: "standard",
      baseline_page_type: "campaigns",
    }, Date.now() - 10_000),
    authorization_id: "8".repeat(32),
  };
  const deferredScopeJob = await scopeRejectAttemptSandbox.runExecutionReadbackAttempt(rejectedScopeJob);
  assert.equal(deferredScopeJob.status, "pending");
  assert.equal(deferredScopeJob.next_attempt_index, 0, "post-reload scope rejection must not consume a readback attempt");
  assert.equal(deferredScopeJob.attempts.length, 0);
  assert.equal(scopeRejectPersisted.length, 2, "the token journal and deferred state must both be durable");

  const committedJob = {
    ...readbackPolicy.createJob({
      action_id: "7".repeat(24),
      store_key: "store-committed",
      account_key: "account-committed",
      plan_id: "plan-committed",
    }, Date.now() - 10_000),
    authorization_id: "6".repeat(32),
    active_readback_token: committedToken,
    active_readback_phase: "snapshot_pending",
  };
  const convergedJob = await committedSandbox.runExecutionReadbackAttempt(committedJob);
  assert.equal(convergedJob.status, "verified", "a committed push lost before its phase update must converge by replaying the same token");
  assert.equal(convergedJob.active_readback_token, "");
  assert.equal(committedCalls.verified, 1);
  assert.equal(committedCalls.persisted, 1);

  const retryableConflictJob = {
    ...readbackPolicy.createJob({
      action_id: "6".repeat(24),
      store_key: "store-conflict",
      account_key: "account-conflict",
      plan_id: "plan-conflict",
    }, Date.now() - 10_000),
    authorization_id: "5".repeat(32),
    active_readback_token: "4d".repeat(32),
    active_readback_phase: "verification_pending",
  };
  const deferredConflict = await retryableConflictSandbox.runExecutionReadbackAttempt(retryableConflictJob);
  assert.equal(deferredConflict.status, "pending");
  assert.equal(deferredConflict.next_attempt_index, 0, "an idempotent 409 in progress must not consume business evidence");
  assert.equal(deferredConflict.active_readback_token, retryableConflictJob.active_readback_token);

  const invalidBodyJob = {
    ...readbackPolicy.createJob({
      action_id: "4".repeat(24),
      store_key: "store-truncated",
      account_key: "account-truncated",
      plan_id: "plan-truncated",
    }, Date.now() - 10_000),
    authorization_id: "3".repeat(32),
    active_readback_token: "2c".repeat(32),
    active_readback_phase: "verification_pending",
  };
  const deferredInvalidBody = await invalidBodyAttemptSandbox.runExecutionReadbackAttempt(invalidBodyJob);
  assert.equal(deferredInvalidBody.status, "pending");
  assert.equal(deferredInvalidBody.next_attempt_index, 0);
  assert.equal(deferredInvalidBody.active_readback_token, invalidBodyJob.active_readback_token, "a truncated 200 body must retain the only replay token");

  const pairingJob = {
    ...readbackPolicy.createJob({
      action_id: "2".repeat(24),
      store_key: "store-pairing",
      account_key: "account-pairing",
      plan_id: "plan-pairing",
    }, Date.now() - 10_000),
    authorization_id: "1".repeat(32),
    active_readback_token: "0b".repeat(32),
    active_readback_phase: "verification_pending",
  };
  const deferredPairing = await pairingFailureSandbox.runExecutionReadbackAttempt(pairingJob);
  assert.equal(deferredPairing.status, "pending");
  assert.equal(deferredPairing.next_attempt_index, 0);
  assert.equal(deferredPairing.active_readback_token, pairingJob.active_readback_token);
  assert.match(deferredPairing.status_label, /配对或会话需要修复/);

  const missingEvidenceJob = {
    ...readbackPolicy.createJob({
      action_id: "3".repeat(24),
      store_key: "store-missing",
      account_key: "account-missing",
      plan_id: "plan-missing",
    }, Date.now() - 10_000),
    authorization_id: "2".repeat(32),
    active_readback_token: missingEvidenceToken,
    active_readback_phase: "snapshot_pending",
  };
  const recoveredMissingEvidence = await missingEvidenceSandbox.runExecutionReadbackAttempt(missingEvidenceJob);
  assert.equal(recoveredMissingEvidence.status, "verified");
  assert.equal(recoveredMissingEvidence.attempts.length, 1, "a 200 without snapshot evidence must not create a phantom business attempt");
  assert.equal(missingEvidenceCalls.verified, 2);
  assert.equal(missingEvidenceCalls.collected, 1);
  assert.equal(missingEvidenceCalls.pushed, 1);

  const unavailableJob = {
    ...readbackPolicy.createJob({
      action_id: "5".repeat(24),
      store_key: "store-unavailable",
      account_key: "account-unavailable",
      plan_id: "plan-unavailable",
    }, Date.now() - 10_000),
    authorization_id: "4".repeat(32),
  };
  const deferredUnavailable = await unavailableSandbox.runExecutionReadbackAttempt(unavailableJob);
  assert.equal(deferredUnavailable.status, "pending");
  assert.equal(deferredUnavailable.next_attempt_index, 0, "Agent HTTP 503 must not consume a business verification attempt");
  assert.equal(deferredUnavailable.attempts.length, 0);
  assert.match(deferredUnavailable.active_readback_token, /^[a-f0-9]{64}$/);

  const contextChangedJob = {
    ...readbackPolicy.createJob({
      action_id: "1".repeat(24),
      store_key: "store-hydrating",
      account_key: "account-hydrating",
      plan_id: "plan-hydrating",
    }, Date.now() - 10_000),
    authorization_id: "0".repeat(32),
  };
  const deferredContextChange = await contextChangedSandbox.runExecutionReadbackAttempt(contextChangedJob);
  assert.equal(deferredContextChange.status, "pending");
  assert.equal(deferredContextChange.next_attempt_index, 0, "SPA hydration/context changes must not consume a business verification attempt");
  assert.equal(deferredContextChange.attempts.length, 0);

  for (const invalidCase of invalidCollectionCases) {
    const replayToken = `${invalidCase.actionHex}d`.repeat(32);
    const invalidCollectionJob = {
      ...readbackPolicy.createJob({
        action_id: invalidCase.actionHex.repeat(24),
        store_key: `store-${invalidCase.tabId}`,
        account_key: `account-${invalidCase.tabId}`,
        plan_id: `plan-${invalidCase.tabId}`,
      }, Date.now() - 10_000),
      authorization_id: `${invalidCase.actionHex}e`.repeat(16),
      active_readback_token: replayToken,
      active_readback_phase: "snapshot_pending",
    };
    const deferredInvalidCollection = await invalidCase.sandbox.runExecutionReadbackAttempt(invalidCollectionJob);
    assert.equal(deferredInvalidCollection.status, "pending", `${invalidCase.name} must remain retryable`);
    assert.equal(deferredInvalidCollection.next_attempt_index, 0, `${invalidCase.name} must not consume a business attempt`);
    assert.equal(deferredInvalidCollection.attempts.length, 0, `${invalidCase.name} must not append phantom evidence`);
    assert.equal(deferredInvalidCollection.active_readback_token, replayToken, `${invalidCase.name} must retain its replay token`);
    assert.equal(deferredInvalidCollection.last_error_code, "READBACK_COLLECTION_RESPONSE_INVALID");
    assert.equal(invalidCase.calls.verified, 1, "the existing token must be probed exactly once before recollection");
    assert.equal(invalidCase.calls.collected, 1);
    assert.equal(invalidCase.calls.pushed, 0);
    assert.equal(invalidCase.calls.persisted.length, 1, "only the deferred state should be persisted for an existing token");
  }

  const multiCandidateJob = {
    ...readbackPolicy.createJob({
      action_id: "d".repeat(24),
      store_key: "store-a",
      account_key: "account-a",
      plan_id: "plan-a",
    }, Date.now() - 10_000),
    authorization_id: "b".repeat(32),
  };
  const verifiedJob = await attemptSandbox.runExecutionReadbackAttempt(multiCandidateJob);
  assert.equal(verifiedJob.status, "verified");
  assert.deepEqual(candidateAttempts, [[], [11]], "a rejected candidate must be excluded before trying the next exact-scope tab");
  assert.equal(pushedOptions.length, 2);
  assert.equal(verifiedCount, 1, "only the accepted candidate may reach action verification");
  assert.equal(persistedAttempts.length, 3, "token, pushed snapshot phase, and terminal verification must be durable");
  assert.equal(collectedOptions.length, 2);
  assert.equal(collectedOptions.every((item) => item.targetPlanId === "plan-a"), true);
  const firstAttemptContexts = pushedOptions.map((item) => item.options.executionContext);
  assert.deepEqual(
    JSON.parse(JSON.stringify(firstAttemptContexts[0])),
    {
      purpose: "execution_readback",
      action_id: "d".repeat(24),
      authorization_id: "b".repeat(32),
      readback_token: firstAttemptContexts[0].readback_token,
    },
  );
  assert.match(firstAttemptContexts[0].readback_token, /^[a-f0-9]{64}$/);
  assert.equal(
    firstAttemptContexts[1].readback_token,
    firstAttemptContexts[0].readback_token,
    "scope fallback candidates within one attempt must share one binding token",
  );
  assert.deepEqual(JSON.parse(JSON.stringify(verificationCalls)), [{
    actionId: "d".repeat(24),
    readbackToken: firstAttemptContexts[0].readback_token,
  }]);
  assert.equal(verifiedJob.last_verification.readback_token, firstAttemptContexts[0].readback_token);
  for (const pushed of pushedOptions) {
    assert.deepEqual(JSON.parse(JSON.stringify(pushed.options)), {
      expectedStoreKey: "store-a",
      expectedAccountKey: "account-a",
      executionContext: {
        purpose: "execution_readback",
        action_id: "d".repeat(24),
        authorization_id: "b".repeat(32),
        readback_token: firstAttemptContexts[0].readback_token,
      },
    });
    assert.equal("runId" in pushed.options, false);
    assert.equal("pageId" in pushed.options, false);
    assert.equal("attemptId" in pushed.options, false);
  }

  const secondAttemptJob = {
    ...readbackPolicy.createJob({
      action_id: "e".repeat(24),
      store_key: "store-a",
      account_key: "account-a",
      plan_id: "plan-b",
    }, Date.now() - 10_000),
    authorization_id: "c".repeat(32),
  };
  const secondVerifiedJob = await attemptSandbox.runExecutionReadbackAttempt(secondAttemptJob);
  assert.equal(secondVerifiedJob.status, "verified");
  const secondAttemptToken = pushedOptions[2].options.executionContext.readback_token;
  assert.match(secondAttemptToken, /^[a-f0-9]{64}$/);
  assert.notEqual(secondAttemptToken, firstAttemptContexts[0].readback_token, "a later attempt must use fresh entropy");
  assert.equal(pushedOptions[3].options.executionContext.readback_token, secondAttemptToken);
  assert.equal(verificationCalls[1].readbackToken, secondAttemptToken);

  const verifyToken = "1a".repeat(32);
  const verified = await verifySandbox.__verifyExecutionAction("f".repeat(24), verifyToken);
  assert.equal(verified.readback_token, verifyToken, "verification results must retain their request binding token");
  assert.deepEqual(JSON.parse(verifyRequests[0].init.body), {
    action_id: "f".repeat(24),
    readback_token: verifyToken,
  });
  await assert.rejects(
    invalidVerifySandbox.__verifyExecutionAction("f".repeat(24), verifyToken),
    (error) => error.code === "READBACK_VERIFY_RESPONSE_INVALID" && error.retryable === true,
  );

  const executionContext = {
    purpose: "execution_readback",
    action_id: "f".repeat(24),
    authorization_id: "a".repeat(32),
    readback_token: verifyToken,
  };
  const pushResult = await storeSandbox.__storeAndPush(
    "qianchuan",
    { page_type: "campaigns", captured_at: 1, quality: { targeted: true } },
    { expectedStoreKey: "store-a", expectedAccountKey: "account-a", executionContext },
  );
  assert.equal(pushResult.ok, true);
  assert.deepEqual(JSON.parse(JSON.stringify(JSON.parse(pushRequests[0].init.body).execution_context)), executionContext);

  const rebuilt = await manualSandbox.__runManualExecutionReadback("9".repeat(24));
  assert.equal(rebuilt.status, "verified");
  assert.equal(manualCalls.bridge.length, 1);
  assert.equal(
    manualCalls.bridge[0].url,
    `http://127.0.0.1:8765/actions/execution/readback-job?action_id=${"9".repeat(24)}`,
  );
  assert.deepEqual(JSON.parse(JSON.stringify(manualCalls.bridge[0].init)), { cache: "no-store" });
  assert.equal(manualCalls.persisted[0].job.reconstructed_from_agent, true);
  assert.equal(manualCalls.persisted[0].job.write_enabled, false);
  assert.equal(manualCalls.persisted[0].job.execution_enabled, false);
  assert.equal(manualCalls.persisted[0].job.baseline_scope_hash, "a".repeat(64));
  assert.equal(manualCalls.persisted[0].job.baseline_document_instance_id, "document-rebuilt-old");
  assert.equal(manualCalls.persisted[0].job.baseline_promotion_mode, "standard");
  assert.equal(manualCalls.persisted[0].job.baseline_page_type, "campaigns");
  assert.equal(manualCalls.readbacks.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(manualCalls.readbacks[0].options)), {
    waitForSchedule: false,
    singleAttempt: true,
    reloadAdmissionInherited: true,
  });
  assert.equal(manualCalls.forbidden.length, 0, "read-only reconstruction must not call execution, preview, or consume endpoints");
  console.log("background execution readback scope tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
