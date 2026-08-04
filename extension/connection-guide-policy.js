(function exposeConnectionGuidePolicy(root, factory) {
  const policy = factory();
  if (typeof module === "object" && module.exports) module.exports = policy;
  root.DianConnectionGuidePolicy = policy;
})(typeof globalThis !== "undefined" ? globalThis : this, function createConnectionGuidePolicy() {
  "use strict";

  function guideView(payload = {}, { qianchuanDeferred = false } = {}) {
    const next = payload.next_upgrade || {};
    return {
      collapsed: Boolean(payload.collapsed) || (Boolean(qianchuanDeferred) && next.id === "sync_qianchuan"),
      actionId: String(next.id || "none"),
      optional: Boolean(next.optional),
      deferred: Boolean(qianchuanDeferred) && next.id === "sync_qianchuan",
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

  return { guideView, automationSurface, automationStep };
});
