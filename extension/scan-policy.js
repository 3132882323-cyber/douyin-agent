/** 店策 Agent - 千川多账号巡检策略 */
(function () {
  "use strict";

  function normalizedLabel(value) {
    return String(value || "").replace(/\s+/g, "").trim().toLocaleLowerCase();
  }

  function sameAccountLabel(left, right) {
    const a = normalizedLabel(left);
    const b = normalizedLabel(right);
    return Boolean(a && b && a === b);
  }

  function matchAccount(captured, expected) {
    if (!expected?.key) return { ok: true, matchedBy: "auto" };
    if (!captured?.key) {
      return {
        ok: false,
        code: "ACCOUNT_UNRESOLVED",
        message: "未识别当前千川账号。巡检已停止刷新，请在当前千川页面确认账号后点击重试。",
      };
    }
    if (captured.key === expected.key) return { ok: true, matchedBy: "key" };

    const sameLabel = sameAccountLabel(captured.label, expected.label);
    const capturedStable = captured.identity_source === "platform_id";
    const expectedStable = expected.identity_source === "platform_id";
    if (sameLabel && !(capturedStable && expectedStable)) {
      return { ok: true, matchedBy: "label" };
    }
    return {
      ok: false,
      code: "ACCOUNT_MISMATCH",
      message: `当前千川账号为“${captured.label || "其他账号"}”，与本轮锁定账号不一致。巡检已停止刷新，请切换账号后重试。`,
    };
  }

  function errorCode(error) {
    if (error?.code) return String(error.code);
    const message = String(error?.message || error || "");
    if (/未识别当前千川账号/.test(message)) return "ACCOUNT_UNRESOLVED";
    if (/与本轮锁定账号不一致|与所选巡查账号不一致/.test(message)) return "ACCOUNT_MISMATCH";
    if (/登录已失效|完成登录/.test(message)) return "LOGIN_REQUIRED";
    return "";
  }

  function isNonRetryable(error) {
    return ["ACCOUNT_UNRESOLVED", "ACCOUNT_MISMATCH", "LOGIN_REQUIRED"].includes(errorCode(error));
  }

  function rankSeedTabs(tabs, preferredTabId = null) {
    return [...(tabs || [])].sort((left, right) => {
      const leftPreferred = left?.id === preferredTabId ? 1 : 0;
      const rightPreferred = right?.id === preferredTabId ? 1 : 0;
      if (leftPreferred !== rightPreferred) return rightPreferred - leftPreferred;
      if (Boolean(left?.active) !== Boolean(right?.active)) return Number(Boolean(right?.active)) - Number(Boolean(left?.active));
      return Number(right?.lastAccessed || 0) - Number(left?.lastAccessed || 0);
    });
  }

  globalThis.DianAgentScanPolicy = {
    sameAccountLabel,
    matchAccount,
    errorCode,
    isNonRetryable,
    rankSeedTabs,
  };
})();
