const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");

function sourceBetween(startMarker, endMarker) {
  const start = script.indexOf(startMarker);
  const end = script.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `${startMarker} should be extractable`);
  return script.slice(start, end);
}

const goals = [
  { value: "profit", checked: false },
  { value: "scale", checked: false },
];
const calculatorInputs = [{ dataset: { chengfangInput: "margin" }, value: "" }];
const evidenceInputs = [{ dataset: { chengfangEvidence: "actual_roi" }, value: "" }];
const boundaryInputs = [{ dataset: { chengfangBoundary: "daily_budget_cap" }, value: "" }];
const goalStatus = { textContent: "" };

const context = {
  console,
  selectedStoreKey: "",
  selectedQianchuanAccount: "",
  currentChengfangLocalPlan: {},
  currentChengfangLocalPlanStore: { schema_version: 5, plans_by_scope: {} },
  currentChengfangAgentRuntime: null,
  currentChengfangA2Pilot: null,
  currentChengfangCandidatePath: null,
  chengfangProfileSyncTimer: null,
  clearTimeout,
  DianChengfangPlanner: { GOALS: { profit: "保利润", scale: "稳规模" } },
  renderChengfangPlanner: () => ({ decision: { action: "只读", evidence: [] } }),
  renderChengfangShadow: () => undefined,
  readChengfangCalculatorInputs: () => Object.fromEntries(calculatorInputs.map((item) => [item.dataset.chengfangInput, item.value])),
  readChengfangEvidenceInputs: () => Object.fromEntries(evidenceInputs.map((item) => [item.dataset.chengfangEvidence, item.value])),
  readChengfangBoundaryInputs: () => Object.fromEntries(boundaryInputs.map((item) => [item.dataset.chengfangBoundary, item.value])),
  document: {
    querySelectorAll: (selector) => {
      if (selector === 'input[name="chengfang-goal"]') return goals;
      if (selector === "[data-chengfang-input]") return calculatorInputs;
      if (selector === "[data-chengfang-evidence]") return evidenceInputs;
      if (selector === "[data-chengfang-boundary]") return boundaryInputs;
      return [];
    },
    querySelector: (selector) => {
      if (selector === 'input[name="chengfang-goal"]:checked') return goals.find((item) => item.checked) || null;
      const match = selector.match(/^input\[name="chengfang-goal"\]\[value="([^"]+)"\]$/);
      return match ? goals.find((item) => item.value === match[1]) || null : null;
    },
    getElementById: (id) => id === "chengfang-goal-status" ? goalStatus : null,
  },
};
context.globalThis = context;

const helperBlock = sourceBetween(
  'const CHENGFANG_LOCAL_PLAN_KEY = "chengfangLocalPlanningV1";',
  "const CHENGFANG_BOUNDARY_LABELS",
);
const restoreBlock = sourceBetween("function restoreChengfangLocalPlan", "\nfunction renderOperationContext");
const buildProfileBlock = sourceBetween("function buildChengfangAgentProfile", "\nfunction candidatePathRuntimeCandidate");
vm.runInNewContext(
  `${helperBlock}\n${restoreBlock}\n${buildProfileBlock}\n`
    + "globalThis.__scopeApi = { chengfangPlanScope, emptyChengfangLocalPlan, normalizeChengfangLocalPlanStore, chengfangPlanForScope, upsertChengfangPlanForScope, removeChengfangPlanForScope, currentChengfangProfileScope, chengfangScopedPostPayload, validateChengfangScopedResponse, restoreChengfangLocalPlan, switchChengfangLocalPlanScope, buildChengfangAgentProfile };",
  context,
  { filename: "chengfang-profile-scope.vm.js" },
);

const api = context.__scopeApi;
const scopeA = api.chengfangPlanScope("store-a", "account-a");
const scopeB = api.chengfangPlanScope("store-b", "account-b");
const scopeA2 = api.chengfangPlanScope("store-a", "account-b");
assert.ok(scopeA.scope_key);
assert.notEqual(scopeA.scope_key, scopeB.scope_key);
assert.notEqual(scopeA.scope_key, scopeA2.scope_key, "accounts within one store must remain isolated");
assert.equal(api.chengfangPlanScope("store-a", "").scope_key, "", "an incomplete scope must fail closed");
assert.equal(api.chengfangPlanScope("", "account-a").scope_key, "", "an incomplete scope must fail closed");

const legacyGlobalDraft = { schema_version: 4, goal: "profit", inputs: { margin: "A" } };
assert.deepEqual(
  Object.keys(api.normalizeChengfangLocalPlanStore(legacyGlobalDraft).plans_by_scope),
  [],
  "the old unscoped global draft must never attach to the currently selected store",
);

function plan(scope, goal, margin, roi, budget) {
  return {
    ...api.emptyChengfangLocalPlan(scope),
    goal,
    inputs: { margin },
    evidence: { actual_roi: roi },
    boundaries: { daily_budget_cap: budget },
  };
}

const planA = plan(scopeA, "profit", "A-margin", "1.8", "500");
const planB = plan(scopeB, "scale", "B-margin", "2.1", "800");
let store = api.upsertChengfangPlanForScope({}, planA);
store = api.upsertChengfangPlanForScope(store, planB);
assert.equal(api.chengfangPlanForScope(store, scopeA).inputs.margin, "A-margin");
assert.equal(api.chengfangPlanForScope(store, scopeB).inputs.margin, "B-margin");
assert.equal(api.chengfangPlanForScope(store, scopeA2), null);

context.currentChengfangLocalPlanStore = store;
context.selectedStoreKey = scopeA.store_key;
context.selectedQianchuanAccount = scopeA.qianchuan_account_id;
api.restoreChengfangLocalPlan(api.chengfangPlanForScope(store, scopeA), { scope: scopeA });
assert.equal(calculatorInputs[0].value, "A-margin");
assert.equal(goals.find((item) => item.value === "profit").checked, true);

context.selectedStoreKey = scopeA2.store_key;
context.selectedQianchuanAccount = scopeA2.qianchuan_account_id;
api.switchChengfangLocalPlanScope(scopeA2.store_key, scopeA2.qianchuan_account_id);
assert.equal(calculatorInputs[0].value, "", "switching to an account without a draft must clear store A values");
assert.equal(evidenceInputs[0].value, "");
assert.equal(boundaryInputs[0].value, "");
assert.equal(goals.some((item) => item.checked), false);

context.selectedStoreKey = scopeB.store_key;
context.selectedQianchuanAccount = scopeB.qianchuan_account_id;
api.switchChengfangLocalPlanScope(scopeB.store_key, scopeB.qianchuan_account_id);
assert.equal(calculatorInputs[0].value, "B-margin");
assert.equal(goals.find((item) => item.value === "scale").checked, true);
context.currentChengfangAgentRuntime = { scope_fingerprint: "runtime-b", decision_automation: { platform_write_enabled: false } };
const requestScope = api.currentChengfangProfileScope();
const profileB = api.buildChengfangAgentProfile(requestScope);
const requestB = api.chengfangScopedPostPayload({ profile: profileB, trigger: "manual" }, requestScope);
assert.equal(requestB.body.profile.inputs.margin, "B-margin");
assert.deepEqual(JSON.parse(JSON.stringify(requestB.body.expected_scope)), {
  store_key: "store-b",
  qianchuan_account_id: "account-b",
  scope_fingerprint: "runtime-b",
  platform_write_enabled: false,
  automatic_submit: false,
});
api.validateChengfangScopedResponse(requestScope, {
  autopilot_runtime: { scope_fingerprint: "runtime-b", decision_automation: { platform_write_enabled: false } },
  platform_write_attempted: false,
});
assert.throws(
  () => api.validateChengfangScopedResponse(requestScope, { autopilot_runtime: { scope_fingerprint: "runtime-a" } }),
  (error) => error.code === "CHENGFANG_SCOPE_RESPONSE_MISMATCH",
);

context.selectedStoreKey = scopeA.store_key;
context.selectedQianchuanAccount = scopeA.qianchuan_account_id;
assert.throws(
  () => api.chengfangScopedPostPayload({ profile: profileB }, requestScope),
  (error) => error.code === "CHENGFANG_SCOPE_CHANGED",
  "a delayed B request must be rejected after switching to A",
);

assert.doesNotMatch(restoreBlock, /syncChengfangProfileToAgent/, "restoring a cached scope must not automatically POST it");
assert.match(script, /switchChengfangLocalPlanScope\(activeKey, scopedAccountKey\)/);
assert.match(script, /restoreChengfangLocalPlan\(\{\}, \{ scope: chengfangPlanScope\("", ""\) \}\)/);
assert.match(script, /expected_scope:[\s\S]{0,260}platform_write_enabled: false[\s\S]{0,120}automatic_submit: false/);

console.log("chengfang scoped profile tests passed");
