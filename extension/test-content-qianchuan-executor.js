const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync(require.resolve("./content-qianchuan.js"), "utf8");
const commonSource = fs.readFileSync(require.resolve("./content-common.js"), "utf8");
const commonContext = {};
commonContext.globalThis = commonContext;
vm.runInNewContext(commonSource, commonContext, { filename: "content-common-plan-token.vm.js" });
const hardenedPseudonymizePlanIdentifier = commonContext.DianAgentExtractor.pseudonymizePlanIdentifier;
let listener;

class MockInput {
  constructor(value, owner = null) {
    this._value = value;
    this.owner = owner;
    this.disabled = false;
    this.style = {};
  }
  get value() { return this._value; }
  set value(next) { inputWriteCount += 1; this._value = next; }
  getClientRects() { return [1]; }
  getAttribute(name) { return name === "aria-label" ? "日预算" : ""; }
  dispatchEvent(event) {
    if (
      replaceRowAfterBudgetWrite
      && this === input
      && event?.type === "change"
      && this.value === "400"
    ) {
      detachedReplacementInput._value = this.value;
      activeRow = detachedReplacementRow;
    }
    if (switchStoreAfterBudgetWrite && event?.type === "change" && this.value === "400") {
      switchStoreAfterBudgetWrite = false;
      setRawIdentity("87654322", "12345678");
    }
    if (switchModeAfterBudgetWrite && event?.type === "change" && this.value === "400") {
      switchModeAfterBudgetWrite = false;
      context.document.body.innerText = "乘方 春季止损计划";
    }
  }
  focus() {}
  scrollIntoView() {}
  closest() { return this.owner || row; }
}

let inputWriteCount = 0;
const input = new MockInput("500");
const duplicateInput = new MockInput("500");
let submitted = false;
let submitClickCount = 0;
let pauseClickCount = 0;
let duplicatePauseClickCount = 0;
let pauseConfirmationClickCount = 0;
let duplicatePauseConfirmationClickCount = 0;
let confirmationDialogVisible = false;
let pauseOpensConfirmationDialog = true;
let duplicatePauseConfirmationButton = false;
let mutateBudgetInputsAfterProbe = false;
let mutateBudgetInputsBeforeSubmit = false;
let budgetInputQueryCount = 0;
let mutatePauseButtonsAfterProbe = false;
let pauseButtonQueryCount = 0;
let replaceRowAfterBudgetWrite = false;
let switchStoreBeforeBudgetWrite = false;
let switchStoreAfterBudgetWrite = false;
let switchModeAfterBudgetWrite = false;
let switchStoreBeforePauseClick = false;
let throwAfterSubmitClick = false;
let resolvedStoreKey = "store_test";
let lastIdentityExpectedScope = null;
let lastIdentityBinding = null;
let lastIdentityExecutionRequest = null;
let lastPageData = null;
let pageDataPushCount = 0;
let identityResolveCount = 0;
let bridgeAwaitMutation = null;
let forcedIdentityError = "";
const submitButton = {
  innerText: "保存",
  textContent: "保存",
  disabled: false,
  getClientRects: () => [1],
  getAttribute: () => null,
  click() {
    submitted = true;
    submitClickCount += 1;
    if (throwAfterSubmitClick) {
      throwAfterSubmitClick = false;
      throw new Error("mock click completion unknown");
    }
  },
};
const pauseButton = {
  innerText: "暂停",
  textContent: "暂停",
  disabled: false,
  getClientRects: () => [1],
  getAttribute: () => null,
  click() {
    pauseClickCount += 1;
    confirmationDialogVisible = pauseOpensConfirmationDialog;
  },
};
const duplicatePauseButton = {
  ...pauseButton,
  click() { duplicatePauseClickCount += 1; },
};
const row = {
  // “暂停”在点击前就存在于操作按钮中，不能作为状态已变更的证据。
  innerText: "计划ID plan_123 春季止损计划 投放中 日预算 500 暂停",
  getClientRects: () => [1],
  querySelectorAll(selector) {
    if (selector === "input") {
      budgetInputQueryCount += 1;
      if (switchStoreBeforeBudgetWrite && budgetInputQueryCount > 1) {
        switchStoreBeforeBudgetWrite = false;
        setRawIdentity("87654322", "12345678");
      }
      const duplicated = (mutateBudgetInputsAfterProbe && budgetInputQueryCount > 1)
        || (mutateBudgetInputsBeforeSubmit && budgetInputQueryCount > 2);
      if (mutateBudgetInputsBeforeSubmit && budgetInputQueryCount > 2) duplicateInput._value = input.value;
      return duplicated ? [input, duplicateInput] : [input];
    }
    if (selector === "button") return [submitButton];
    if (selector === "button, [role='button'], [role='switch']") {
      pauseButtonQueryCount += 1;
      if (switchStoreBeforePauseClick && pauseButtonQueryCount > 1) {
        switchStoreBeforePauseClick = false;
        setRawIdentity("87654322", "12345678");
      }
      return mutatePauseButtonsAfterProbe && pauseButtonQueryCount > 1 ? [pauseButton, duplicatePauseButton] : [pauseButton];
    }
    return [];
  },
};
let detachedReplacementInput;
const detachedReplacementRow = {
  innerText: row.innerText,
  getClientRects: () => [1],
  querySelectorAll(selector) {
    if (selector === "input") return [detachedReplacementInput];
    if (selector === "button") return [];
    if (selector === "button, [role='button'], [role='switch']") return [pauseButton];
    return [];
  },
};
detachedReplacementInput = new MockInput("500", detachedReplacementRow);
let activeRow = row;
let executionRows = null;
const staleSuccessToast = {
  innerText: "上一项修改成功",
  textContent: "上一项修改成功",
  getClientRects: () => [1],
};
const cancelPauseConfirmationButton = {
  innerText: "取消",
  textContent: "取消",
  disabled: false,
  getClientRects: () => confirmationDialogVisible ? [1] : [],
  getAttribute: () => null,
  click() {},
};
const pauseConfirmationButton = {
  innerText: "确认暂停",
  textContent: "确认暂停",
  disabled: false,
  getClientRects: () => confirmationDialogVisible ? [1] : [],
  getAttribute: () => null,
  click() {
    pauseConfirmationClickCount += 1;
    confirmationDialogVisible = false;
  },
};
const secondPauseConfirmationButton = {
  ...pauseConfirmationButton,
  innerText: "确定暂停",
  textContent: "确定暂停",
  click() { duplicatePauseConfirmationClickCount += 1; },
};
const confirmationDialog = {
  innerText: "请确认是否暂停该计划",
  textContent: "请确认是否暂停该计划",
  getClientRects: () => confirmationDialogVisible ? [1] : [],
  querySelectorAll(selector) {
    if (selector === "button, [role='button']") {
      return duplicatePauseConfirmationButton
        ? [cancelPauseConfirmationButton, pauseConfirmationButton, secondPauseConfirmationButton]
        : [cancelPauseConfirmationButton, pauseConfirmationButton];
    }
    return [];
  },
};
const context = {
  URLSearchParams,
  location: {
    href: "https://qianchuan.jinritemai.com/uni-prom?shop_id=87654321&advertiser_id=12345678",
    pathname: "/uni-prom",
    search: "?shop_id=87654321&advertiser_id=12345678",
    hash: "",
    hostname: "qianchuan.jinritemai.com",
  },
  document: {
    title: "巨量千川",
    body: { innerText: "标准推广 春季止损计划" },
    documentElement: {},
    querySelectorAll(selector) {
      if (selector.includes("table-row") || selector.startsWith("tr,")) return executionRows || [activeRow];
      if (selector.includes("shopName")) return [{ innerText: "测试店铺", getClientRects: () => [1] }];
      if (selector.includes("[role='alert']")) return [staleSuccessToast];
      if (selector.includes("[role='dialog']")) return confirmationDialogVisible ? [confirmationDialog] : [];
      return [];
    },
  },
  chrome: {
    storage: { local: { get: async () => ({ settings: { privacyMode: true } }) } },
    runtime: {
      onMessage: { addListener(callback) { listener = callback; } },
      sendMessage: async (message) => {
        if (message.type === "page-data") pageDataPushCount += 1;
        if (message.type === "resolve-execution-identity") identityResolveCount += 1;
        lastIdentityExpectedScope = message.expected_scope || null;
        lastIdentityBinding = message.type === "resolve-execution-identity" ? {
          action_id: message.action_id,
          authorization_id: message.authorization_id,
        } : null;
        lastIdentityExecutionRequest = message.type === "resolve-execution-identity"
          ? message.execution_request
          : null;
        lastPageData = message.data || message.snapshot || null;
        if (message.type === "resolve-execution-identity" && forcedIdentityError) {
          const error = forcedIdentityError;
          forcedIdentityError = "";
          return { ok: false, error };
        }
        const response = {
          ok: true,
          execution_request_bound: true,
          execution_request_digest: "d".repeat(64),
          account: { key: "adacct_test", identity_source: "hmac_qianchuan_advertiser_id" },
          store: { key: resolvedStoreKey, identity_source: "hmac_qianchuan_shop_id" },
        };
        if (bridgeAwaitMutation) {
          const mutate = bridgeAwaitMutation;
          bridgeAwaitMutation = null;
          await Promise.resolve();
          mutate();
        }
        return response;
      },
    },
  },
  sessionStorage: { length: 0, key: () => null, getItem: () => null },
  localStorage: { length: 0, key: () => null, getItem: () => null },
  DianAgentExtractor: {
    collect: async (sourceName, pageType) => ({ quality: { score: 90 }, page_type: pageType }),
    pseudonymizePlanIdentifier: hardenedPseudonymizePlanIdentifier,
  },
  HTMLInputElement: MockInput,
  Event: class { constructor(type) { this.type = type; } },
  MutationObserver: class { observe() {} },
  setTimeout(callback, delay) { if (delay < 1000) callback(); return 1; },
  clearTimeout() {},
  console,
};
context.globalThis = context;
vm.runInNewContext(source, context, { filename: "content-qianchuan.js" });

function setRawIdentity(storeId = "87654321", advertiserId = "12345678") {
  const search = `?shop_id=${storeId}&advertiser_id=${advertiserId}`;
  context.location.search = search;
  context.location.href = `https://qianchuan.jinritemai.com/uni-prom${search}`;
}

function send(request, type = "qianchuan-supervised-submit") {
  return new Promise((resolve) => {
    listener({
      type,
      request: {
        ...request,
        authorization_id: request.authorization_id ?? request.execution_attempt_id,
      },
    }, {}, resolve);
  });
}

(async () => {
  const accountKey = "adacct_test"; // Local bridge-resolved HMAC account key.
  const request = {
    action_id: "a".repeat(24),
    operation_type: "adjust_budget",
    mode: "supervised_submit",
    account_key: accountKey,
    page_type: "campaigns",
    plan_id: "plan_123",
    plan_name: "春季止损计划",
    expected_current_value: 500,
    target_value: 400,
    execution_attempt_id: "f".repeat(32),
    execute_before_ms: Date.now() + 60_000,
    promotion_context: { promotion_mode: "standard", account_scope: { store_id: "store_test", account_id: accountKey }, strategy_id: "strategy_test", metric_contract: { version: "v1" } },
  };
  const writesBeforeBindingMismatch = inputWriteCount;
  const clicksBeforeBindingMismatch = submitClickCount;
  const resolvesBeforeBindingMismatch = identityResolveCount;
  const sharedAuthorizationId = "e".repeat(32);
  const mismatchedGrantOne = await send({
    ...request,
    authorization_id: sharedAuthorizationId,
    execution_attempt_id: "1f".repeat(16),
  });
  const mismatchedGrantTwo = await send({
    ...request,
    authorization_id: sharedAuthorizationId,
    execution_attempt_id: "2f".repeat(16),
  });
  assert.equal(mismatchedGrantOne.ok, false);
  assert.equal(mismatchedGrantTwo.ok, false);
  assert.equal(mismatchedGrantOne.code, "EXECUTION_GRANT_BINDING_INVALID");
  assert.equal(inputWriteCount, writesBeforeBindingMismatch, "mismatched authorization aliases must stop before input mutation");
  assert.equal(submitClickCount, clicksBeforeBindingMismatch, "one authorization paired with alternate attempt ids must remain zero-click");
  assert.equal(identityResolveCount, resolvesBeforeBindingMismatch, "invalid grant aliases must stop before the Agent identity request");

  forcedIdentityError = "执行页面请求的计划、动作、预算或授权参数已变化，页面未提交。";
  const writesBeforeTamperedPlan = inputWriteCount;
  const clicksBeforeTamperedPlan = submitClickCount;
  const tamperedPlan = await send({
    ...request,
    execution_attempt_id: "3f".repeat(16),
    plan_id: "plan_other",
    plan_name: "同账户另一计划",
  });
  assert.equal(tamperedPlan.ok, false);
  assert.match(tamperedPlan.error, /计划、动作、预算或授权参数已变化/);
  assert.equal(inputWriteCount, writesBeforeTamperedPlan, "Agent request binding must reject a changed plan before mutation");
  assert.equal(submitClickCount, clicksBeforeTamperedPlan, "Agent request binding must reject a changed plan before click");
  const crossAccount = await send({
    ...request,
    execution_attempt_id: "01".repeat(16),
    promotion_context: {
      ...request.promotion_context,
      account_scope: { store_id: "store_test", account_id: "adacct_other" },
    },
  });
  assert.equal(crossAccount.ok, false);
  assert.match(crossAccount.error, /ACTION_CONTEXT_ACCOUNT_MISMATCH/);
  assert.equal(inputWriteCount, 0);
  assert.equal(submitClickCount, 0);
  assert.equal(input.value, "500");

  resolvedStoreKey = "store_other";
  const writesBeforeCrossStore = inputWriteCount;
  const clicksBeforeCrossStore = submitClickCount;
  const pageDataBeforeCrossStoreProbe = pageDataPushCount;
  const resolvesBeforeCrossStoreProbe = identityResolveCount;
  const crossStoreProbe = await send(request, "qianchuan-execution-probe");
  assert.equal(crossStoreProbe.ready, false);
  assert.match(crossStoreProbe.error, /当前店铺与授权店铺不一致/);
  assert.equal(
    pageDataPushCount,
    pageDataBeforeCrossStoreProbe,
    "an execution identity probe is ephemeral and must not overwrite canonical page-data",
  );
  assert.equal(
    identityResolveCount,
    resolvesBeforeCrossStoreProbe + 1,
    "an execution identity probe must use the background ephemeral resolver",
  );
  assert.equal(lastIdentityExpectedScope, null, "ephemeral resolution must use the authorization binding, not canonical push scope fields");
  assert.deepEqual(JSON.parse(JSON.stringify(lastIdentityBinding)), {
    action_id: request.action_id,
    authorization_id: request.execution_attempt_id,
  });
  assert.equal(lastIdentityExecutionRequest.plan_id, request.plan_id);
  assert.equal(lastIdentityExecutionRequest.authorization_id, request.execution_attempt_id);
  assert.equal(lastIdentityExecutionRequest.execution_attempt_id, request.execution_attempt_id);
  const crossStoreSubmit = await send(request);
  assert.equal(crossStoreSubmit.ok, false);
  assert.equal(crossStoreSubmit.submitted, false);
  assert.match(crossStoreSubmit.error, /当前店铺与授权店铺不一致/);
  assert.equal(inputWriteCount, writesBeforeCrossStore, "cross-store reuse of the same account and plan must stop before any write");
  assert.equal(submitClickCount, clicksBeforeCrossStore, "cross-store reuse of the same account and plan must stop before click");
  assert.equal(input.value, "500");
  resolvedStoreKey = "store_test";

  const firstDocumentInstanceId = lastPageData.document_instance_id;
  assert.match(firstDocumentInstanceId, /^(?:[0-9a-f-]{32,36}|doc-[a-z0-9-]+)$/i);
  assert.equal(lastPageData.document_url, context.location.href);
  assert.ok(Number.isFinite(lastPageData.navigation_started_at_ms));

  const writesBeforeBridgeRace = inputWriteCount;
  const clicksBeforeBridgeRace = submitClickCount;
  bridgeAwaitMutation = () => setRawIdentity("87654322", "12345678");
  const bridgeRace = await send({
    ...request,
    execution_attempt_id: "1".repeat(32),
    execute_before_ms: Date.now() + 60_000,
  });
  assert.equal(bridgeRace.ok, false);
  assert.equal(bridgeRace.submitted, false, "a bridge-await witness failure is proven pre-mutation");
  assert.match(bridgeRace.error, /EXECUTION_PAGE_WITNESS_CHANGED/);
  assert.equal(inputWriteCount, writesBeforeBridgeRace);
  assert.equal(submitClickCount, clicksBeforeBridgeRace);
  assert.equal(input.value, "500");
  setRawIdentity();

  const stableDocumentProbe = await send(request, "qianchuan-execution-probe");
  assert.equal(stableDocumentProbe.ready, true);
  assert.equal(pageDataPushCount, 0, "execution probes must never emit a canonical page-data push");
  assert.equal(lastPageData.document_instance_id, firstDocumentInstanceId, "document ID must remain stable for the page lifetime");
  assert.equal(lastPageData.document_url, context.location.href);

  switchStoreBeforeBudgetWrite = true;
  budgetInputQueryCount = 0;
  const writesBeforeSynchronousStoreSwitch = inputWriteCount;
  const clicksBeforeSynchronousStoreSwitch = submitClickCount;
  const synchronousStoreSwitch = await send({
    ...request,
    execution_attempt_id: "2".repeat(32),
    execute_before_ms: Date.now() + 60_000,
  });
  assert.equal(synchronousStoreSwitch.ok, false);
  assert.equal(synchronousStoreSwitch.submitted, false);
  assert.match(synchronousStoreSwitch.error, /EXECUTION_PAGE_WITNESS_CHANGED/);
  assert.equal(inputWriteCount, writesBeforeSynchronousStoreSwitch, "A-to-B identity switch before the first DOM write must perform zero writes");
  assert.equal(submitClickCount, clicksBeforeSynchronousStoreSwitch);
  assert.equal(input.value, "500");
  setRawIdentity();
  budgetInputQueryCount = 0;

  const budgetSubstringMismatch = await send({
    ...request,
    plan_id: "plan_12",
  }, "qianchuan-execution-probe");
  assert.equal(budgetSubstringMismatch.ready, false);
  assert.match(budgetSubstringMismatch.error, /未找到授权计划/);
  assert.equal(input.value, "500");

  const pauseSubstringMismatch = await send({
    action_id: request.action_id,
    execution_attempt_id: "02".repeat(16),
    operation_type: "pause_plan",
    mode: "supervised_submit",
    account_key: accountKey,
    page_type: "campaigns",
    plan_id: "plan_12",
    plan_name: "春季止损计划",
    expected_current_value: "投放中",
    target_value: "暂停",
    promotion_context: { promotion_mode: "standard", account_scope: { store_id: "store_test", account_id: accountKey }, strategy_id: "strategy_test", metric_contract: { version: "v1" } },
  }, "qianchuan-execution-probe");
  assert.equal(pauseSubstringMismatch.ready, false);
  assert.match(pauseSubstringMismatch.error, /未找到授权计划/);

  const legacyCollisionTargetId = "578088328377115042";
  const legacyCollisionSiblingId = "496758059572559982";
  const collisionTargetToken = hardenedPseudonymizePlanIdentifier(legacyCollisionTargetId, "计划ID");
  const collisionSiblingToken = hardenedPseudonymizePlanIdentifier(legacyCollisionSiblingId, "计划ID");
  assert.notEqual(collisionTargetToken, collisionSiblingToken, "known legacy FNV collision IDs must not share an execution token");
  const collisionRow = (planId) => ({
    innerText: `计划ID ${planId} 春季止损计划 投放中 日预算 500 暂停`,
    getClientRects: () => [1],
    querySelectorAll: () => [],
  });
  const writesBeforeCollisionProbe = inputWriteCount;
  const clicksBeforeCollisionProbe = submitClickCount;
  executionRows = [collisionRow(legacyCollisionSiblingId)];
  const absentCollisionTarget = await send({
    ...request,
    plan_id: collisionTargetToken,
  }, "qianchuan-execution-probe");
  assert.equal(absentCollisionTarget.ready, false);
  assert.match(absentCollisionTarget.error, /未找到授权计划/);
  assert.equal(inputWriteCount, writesBeforeCollisionProbe, "a colliding same-name sibling must not receive the target plan's write");
  assert.equal(submitClickCount, clicksBeforeCollisionProbe, "a colliding same-name sibling must fail closed before click");

  executionRows = [collisionRow(legacyCollisionTargetId), collisionRow(legacyCollisionTargetId)];
  const duplicateSameNameTarget = await send({
    ...request,
    plan_id: collisionTargetToken,
  }, "qianchuan-execution-probe");
  assert.equal(duplicateSameNameTarget.ready, false);
  assert.match(duplicateSameNameTarget.error, /多个同名计划/);
  assert.equal(inputWriteCount, writesBeforeCollisionProbe, "duplicate same-name identity must fail before any write");
  assert.equal(submitClickCount, clicksBeforeCollisionProbe, "duplicate same-name identity must fail before any click");
  executionRows = null;

  const probe = await send(request, "qianchuan-execution-probe");
  assert.equal(probe.ready, true);
  assert.equal(input.value, "500");

  mutateBudgetInputsAfterProbe = true;
  budgetInputQueryCount = 0;
  duplicateInput._value = "500";
  const writesBeforeBudgetMutation = inputWriteCount;
  const clicksBeforeBudgetMutation = submitClickCount;
  const budgetDomChanged = await send({
    ...request,
    execution_attempt_id: "b".repeat(32),
    execute_before_ms: Date.now() + 60_000,
  });
  assert.equal(budgetDomChanged.ok, false);
  assert.match(budgetDomChanged.error, /执行前发现多个预算输入框/);
  assert.equal(inputWriteCount, writesBeforeBudgetMutation, "DOM ambiguity after probe must stop before the first write");
  assert.equal(submitClickCount, clicksBeforeBudgetMutation);
  assert.equal(input.value, "500");
  mutateBudgetInputsAfterProbe = false;
  budgetInputQueryCount = 0;

  mutateBudgetInputsBeforeSubmit = true;
  budgetInputQueryCount = 0;
  const clicksBeforeSubmitMutation = submitClickCount;
  const budgetDomChangedBeforeClick = await send({
    ...request,
    execution_attempt_id: "c".repeat(32),
    execute_before_ms: Date.now() + 60_000,
  });
  assert.equal(budgetDomChangedBeforeClick.ok, false);
  assert.equal(budgetDomChangedBeforeClick.code, "BUDGET_RECOVERY_UNVERIFIED");
  assert.equal(budgetDomChangedBeforeClick.recovery_unverified, true);
  assert.equal(budgetDomChangedBeforeClick.submitted, null, "an unverified rollback must never claim submitted=false");
  assert.equal(budgetDomChangedBeforeClick.submission_state, "unknown");
  assert.match(budgetDomChangedBeforeClick.error, /无法证明页面已恢复原值/);
  assert.equal(submitClickCount, clicksBeforeSubmitMutation);
  assert.equal(input.value, "400", "ambiguous live targets must not be mutated under a false rollback claim");
  mutateBudgetInputsBeforeSubmit = false;
  budgetInputQueryCount = 0;
  input._value = "500";
  duplicateInput._value = "500";

  switchStoreAfterBudgetWrite = true;
  const clicksBeforePostWriteStoreSwitch = submitClickCount;
  const writesBeforePostWriteStoreSwitch = inputWriteCount;
  const postWriteStoreSwitch = await send({
    ...request,
    execution_attempt_id: "3".repeat(32),
    execute_before_ms: Date.now() + 60_000,
  });
  assert.equal(postWriteStoreSwitch.ok, false);
  assert.equal(postWriteStoreSwitch.submitted, null, "a post-write identity switch has unknown submission state");
  assert.equal(postWriteStoreSwitch.submission_state, "unknown");
  assert.equal(postWriteStoreSwitch.code, "BUDGET_RECOVERY_UNVERIFIED");
  assert.equal(submitClickCount, clicksBeforePostWriteStoreSwitch, "A-to-B identity switch after input must still perform zero clicks");
  assert.equal(inputWriteCount, writesBeforePostWriteStoreSwitch + 1, "A-to-B identity switch must never write into the new account during cleanup");
  assert.equal(input.value, "400", "an identity change forbids even a cleanup write; result remains unknown and locked");
  setRawIdentity();
  input._value = "500";

  switchModeAfterBudgetWrite = true;
  context.document.body.innerText = "标准推广 春季止损计划";
  const clicksBeforeChengfangSwitch = submitClickCount;
  const writesBeforeChengfangSwitch = inputWriteCount;
  const chengfangSwitch = await send({
    ...request,
    execution_attempt_id: "4".repeat(32),
    execute_before_ms: Date.now() + 60_000,
  });
  assert.equal(chengfangSwitch.ok, false);
  assert.equal(chengfangSwitch.submitted, null);
  assert.equal(chengfangSwitch.submission_state, "unknown");
  assert.equal(chengfangSwitch.code, "BUDGET_RECOVERY_UNVERIFIED");
  assert.equal(submitClickCount, clicksBeforeChengfangSwitch, "switching to Chengfang after input must never click legacy submit");
  assert.equal(inputWriteCount, writesBeforeChengfangSwitch + 1, "mode switch must not perform a cleanup write in Chengfang");
  assert.equal(input.value, "400", "mode switch keeps the action locked instead of writing across promotion modes");
  context.document.body.innerText = "标准推广 春季止损计划";
  input._value = "500";

  replaceRowAfterBudgetWrite = true;
  activeRow = row;
  detachedReplacementInput._value = "500";
  const clicksBeforeDetachedReplacement = submitClickCount;
  const detachedReplacement = await send({
    ...request,
    execution_attempt_id: "9".repeat(32),
    execute_before_ms: Date.now() + 60_000,
  });
  assert.equal(detachedReplacement.ok, false);
  assert.equal(detachedReplacement.submitted, null, "any failure after a budget write is conservatively unknown");
  assert.equal(detachedReplacement.submission_state, "unknown");
  assert.equal(detachedReplacement.recovery_unverified, false);
  assert.match(detachedReplacement.error, /未找到唯一提交按钮，已停止执行/);
  assert.equal(input.value, "400", "the detached node is not authoritative after the virtual row is replaced");
  assert.equal(detachedReplacementInput.value, "500", "rollback must restore and verify the current live input");
  assert.equal(submitClickCount, clicksBeforeDetachedReplacement, "a detached replacement path must never click submit");
  replaceRowAfterBudgetWrite = false;
  activeRow = row;
  input._value = "500";
  budgetInputQueryCount = 0;

  throwAfterSubmitClick = true;
  const clicksBeforeThrowingSubmit = submitClickCount;
  const throwingSubmit = await send({
    ...request,
    execution_attempt_id: "6".repeat(32),
    execute_before_ms: Date.now() + 60_000,
  });
  assert.equal(throwingSubmit.ok, false);
  assert.equal(throwingSubmit.submitted, null, "an exception after invoking click must be submission_unknown");
  assert.equal(throwingSubmit.submission_state, "unknown");
  assert.equal(submitClickCount, clicksBeforeThrowingSubmit + 1);
  assert.equal(input.value, "400", "post-click errors must not attempt an unsafe local rollback");
  input._value = "500";

  const clicksBeforeExpiredGrant = submitClickCount;
  const expiredGrant = await send({
    ...request,
    execution_attempt_id: "7".repeat(32),
    execute_before_ms: Date.now() - 1,
  });
  assert.equal(expiredGrant.ok, false);
  assert.match(expiredGrant.error, /单次执行授权已过期/);
  assert.equal(submitClickCount, clicksBeforeExpiredGrant);
  const successfulRequest = {
    ...request,
    execution_attempt_id: "8".repeat(32),
    execute_before_ms: Date.now() + 60_000,
  };
  const result = await send(successfulRequest);
  assert.equal(result.ok, true);
  assert.equal(result.submitted, true);
  assert.equal(result.submitted_unverified, true);
  assert.equal(result.platform_success_observed, false, "a stale success toast must never verify the current submission");
  assert.equal(result.verification_required, true);
  assert.equal(result.submission_evidence, "dom_click_only");
  assert.equal(input.value, "400");

  const writesBeforeDuplicateAttempt = inputWriteCount;
  const clicksBeforeDuplicateAttempt = submitClickCount;
  const duplicateAttempt = await send(successfulRequest);
  assert.equal(duplicateAttempt.ok, false);
  assert.equal(duplicateAttempt.code, "DUPLICATE_EXECUTION_ATTEMPT");
  assert.equal(duplicateAttempt.submitted, null, "a replayed attempt has unknown external state and must remain locked");
  assert.equal(duplicateAttempt.submission_state, "unknown");
  assert.equal(inputWriteCount, writesBeforeDuplicateAttempt, "a replayed attempt must not write a second time");
  assert.equal(submitClickCount, clicksBeforeDuplicateAttempt, "a replayed attempt must not click a second time");

  const pauseRequest = {
    ...request,
    operation_type: "pause_plan",
    expected_current_value: "投放中",
    target_value: "暂停",
    execution_attempt_id: "d".repeat(32),
    execute_before_ms: Date.now() + 60_000,
  };
  mutatePauseButtonsAfterProbe = true;
  pauseButtonQueryCount = 0;
  const pauseClicksBeforeMutation = pauseClickCount;
  const changedPauseDom = await send({
    ...pauseRequest,
    execution_attempt_id: "a".repeat(32),
  });
  assert.equal(changedPauseDom.ok, false);
  assert.match(changedPauseDom.error, /执行前发现多个暂停按钮/);
  assert.equal(pauseClickCount, pauseClicksBeforeMutation, "DOM ambiguity after probe must stop before click");
  assert.equal(duplicatePauseClickCount, 0);
  mutatePauseButtonsAfterProbe = false;
  pauseButtonQueryCount = 0;

  switchStoreBeforePauseClick = true;
  const pauseClicksBeforeStoreSwitch = pauseClickCount;
  const pauseStoreSwitch = await send({
    ...pauseRequest,
    execution_attempt_id: "5".repeat(32),
  });
  assert.equal(pauseStoreSwitch.ok, false);
  assert.equal(pauseStoreSwitch.submitted, false, "identity changed before pause click is proven not submitted");
  assert.match(pauseStoreSwitch.error, /EXECUTION_PAGE_WITNESS_CHANGED/);
  assert.equal(pauseClickCount, pauseClicksBeforeStoreSwitch);
  setRawIdentity();
  pauseButtonQueryCount = 0;

  const pauseResult = await send(pauseRequest);
  assert.equal(pauseResult.ok, true);
  assert.equal(pauseClickCount, 1);
  assert.equal(pauseConfirmationClickCount, 1, "the unique second confirmation should be clicked exactly once");
  assert.equal(confirmationDialogVisible, false, "the second confirmation dialog should be completed");
  assert.equal(pauseResult.submitted, true);
  assert.equal(pauseResult.submitted_unverified, true);
  assert.equal(pauseResult.platform_success_observed, false, "an existing pause label or confirmation dialog is not a readback");
  assert.equal(pauseResult.verification_required, true);
  assert.equal(pauseResult.submission_evidence, "dom_pause_click_and_confirmation");

  pauseOpensConfirmationDialog = false;
  const directPauseResult = await send({ ...pauseRequest, execution_attempt_id: "0e".repeat(16) });
  assert.equal(directPauseResult.ok, true, "a platform variant without a second modal remains compatible");
  assert.equal(pauseClickCount, 2);
  assert.equal(pauseConfirmationClickCount, 1);
  assert.equal(directPauseResult.submission_evidence, "dom_pause_click_no_confirmation_observed");
  pauseOpensConfirmationDialog = true;

  duplicatePauseConfirmationButton = true;
  const ambiguousConfirmation = await send({ ...pauseRequest, execution_attempt_id: "0f".repeat(16) });
  assert.equal(ambiguousConfirmation.ok, false);
  assert.equal(ambiguousConfirmation.submitted, null, "ambiguity after the first click must never invite an automatic retry");
  assert.match(ambiguousConfirmation.error, /多个确认按钮/);
  assert.equal(pauseConfirmationClickCount, 1);
  assert.equal(duplicatePauseConfirmationClickCount, 0);
  confirmationDialogVisible = false;
  duplicatePauseConfirmationButton = false;

  input.value = "450";
  const mismatch = await send({
    action_id: request.action_id,
    operation_type: "adjust_budget",
    mode: "supervised_submit",
    account_key: accountKey,
    page_type: "campaigns",
    plan_id: "plan_123",
    plan_name: "春季止损计划",
    expected_current_value: 500,
    target_value: 400,
    execution_attempt_id: "e".repeat(32),
    execute_before_ms: Date.now() + 60_000,
    promotion_context: { promotion_mode: "standard", account_scope: { store_id: "store_test", account_id: accountKey }, strategy_id: "strategy_test", metric_contract: { version: "v1" } },
  });
  assert.equal(mismatch.ok, false);
  assert.match(mismatch.error, /当前预算一致/);
  assert.equal(input.value, "450");

  const chengfang = await send({
    ...request,
    execution_attempt_id: "ab".repeat(16),
    promotion_context: { promotion_mode: "chengfang" },
  });
  assert.equal(chengfang.ok, false);
  assert.match(chengfang.error, /UNSUPPORTED_FOR_CHENGFANG/);
  assert.equal(input.value, "450");
  console.log("content-qianchuan executor tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
