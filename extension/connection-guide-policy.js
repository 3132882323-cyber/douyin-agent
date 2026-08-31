(function exposeConnectionGuidePolicy(root, factory) {
  const policy = factory();
  if (typeof module === "object" && module.exports) module.exports = policy;
  root.DianConnectionGuidePolicy = policy;
})(typeof globalThis !== "undefined" ? globalThis : this, function createConnectionGuidePolicy() {
  "use strict";

  function guideView(payload = {}, { qianchuanDeferred = false } = {}) {
    const next = payload.next_upgrade || {};
    const operational = payload.operational && typeof payload.operational === "object" ? payload.operational : {};
    const operationalState = String(operational.state || "");
    return {
      collapsed: Boolean(payload.collapsed) || (Boolean(qianchuanDeferred) && next.id === "sync_qianchuan"),
      actionId: String(next.id || "none"),
      optional: Boolean(next.optional),
      deferred: Boolean(qianchuanDeferred) && next.id === "sync_qianchuan",
      operationalState,
      operationalLabel: String(operational.state_label || ""),
      currentlyReady: operational.core_data_fresh === true
        && ["data_fresh", "execution_ready"].includes(operationalState),
    };
  }

  function automationSurface({ selectedAccountKey = "", itemCount = 0, deferred = false } = {}) {
    if (!String(selectedAccountKey)) return deferred ? "deferred" : "off";
    return Number(itemCount || 0) > 0 ? "candidates" : "no_plans";
  }

  function automationStep(state = "idle") {
    if (["executed", "verified", "completed"].includes(String(state))) return "result";
    if (String(state) !== "idle") return "authorization";
    return "proposal";
  }

  function bindingReview({
    selectedStoreKey = "",
    unlinkedAccounts = [],
    qianchuanDeferred = false,
    currentActionId = "",
  } = {}) {
    return Boolean(String(selectedStoreKey))
      && Array.isArray(unlinkedAccounts)
      && unlinkedAccounts.length > 0
      && !Boolean(qianchuanDeferred)
      && String(currentActionId) === "sync_qianchuan";
  }

  return { guideView, automationSurface, automationStep, bindingReview };
});
