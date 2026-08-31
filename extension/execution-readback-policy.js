(function initExecutionReadbackPolicy(root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.DianExecutionReadbackPolicy = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function buildExecutionReadbackPolicy() {
  "use strict";

  const OFFSETS_MS = Object.freeze([2_000, 5_000, 15_000, 30_000]);
  const TERMINAL_STATES = new Set(["verified", "failed", "uncertain"]);

  function normalizeScopeValue(value, maxLength = 128) {
    const normalized = String(value || "").trim().toLowerCase();
    if (!normalized || normalized.length > maxLength || /[\s\u0000-\u001f\u007f]/.test(normalized)) return "";
    return normalized;
  }

  function normalizedScope(input = {}) {
    return {
      store_key: normalizeScopeValue(input.store_key ?? input.storeKey, 80),
      account_key: normalizeScopeValue(input.account_key ?? input.accountKey, 128),
      plan_id: normalizeScopeValue(input.plan_id ?? input.planId, 128),
    };
  }

  function scopeIsComplete(scope = {}) {
    return Boolean(scope.store_key && scope.account_key && scope.plan_id);
  }

  function createJob(input = {}, nowMs = Date.now()) {
    const actionId = String(input.action_id || input.actionId || "").trim().toLowerCase();
    if (!/^[a-f0-9]{24}$/.test(actionId)) throw new Error("回读任务缺少有效动作编号");
    const scope = normalizedScope(input);
    if (!scopeIsComplete(scope)) throw new Error("回读任务必须绑定店铺、千川账户和计划");
    const startedAtMs = Math.max(1, Number(input.started_at_ms || input.startedAtMs || nowMs));
    const rawTabId = input.tab_id ?? input.tabId;
    const rawWindowId = input.window_id ?? input.windowId;
    const baselineDocumentId = String(input.baseline_document_instance_id || "").trim();
    const baselineScopeHash = String(input.baseline_scope_hash || "").trim().toLowerCase();
    const baselinePromotionMode = String(input.baseline_promotion_mode || "unknown").trim().toLowerCase();
    const rawPageType = String(input.baseline_page_type || "unknown").trim().toLowerCase();
    const baselinePageType = rawPageType === "qianchuan_campaigns" ? "campaigns" : rawPageType;
    if (baselineDocumentId && !/^[A-Za-z0-9_-]{16,128}$/.test(baselineDocumentId)) {
      throw new Error("回读任务的执行前文档标识无效");
    }
    if (baselineScopeHash && !/^[a-f0-9]{64}$/.test(baselineScopeHash)) {
      throw new Error("回读任务的页面范围指纹无效");
    }
    if (!new Set(["standard", "full_domain", "unknown"]).has(baselinePromotionMode)) {
      throw new Error("回读任务的投放模式无效");
    }
    if (!new Set(["campaigns", "qianchuan_live", "unknown"]).has(baselinePageType)) {
      throw new Error("回读任务的计划页面类型无效");
    }
    return {
      schema_version: 2,
      action_id: actionId,
      ...scope,
      tab_id: rawTabId !== null && rawTabId !== undefined && rawTabId !== "" && Number.isInteger(Number(rawTabId)) ? Number(rawTabId) : null,
      window_id: rawWindowId !== null && rawWindowId !== undefined && rawWindowId !== "" && Number.isInteger(Number(rawWindowId)) ? Number(rawWindowId) : null,
      baseline_document_instance_id: baselineDocumentId,
      baseline_navigation_started_at_ms: Math.max(0, Number(input.baseline_navigation_started_at_ms) || 0),
      baseline_scope_hash: baselineScopeHash,
      baseline_promotion_mode: baselinePromotionMode,
      baseline_page_type: baselinePageType,
      started_at_ms: startedAtMs,
      offsets_ms: [...OFFSETS_MS],
      next_attempt_index: 0,
      attempts: [],
      status: "pending",
      status_label: "等待平台回读",
      deferred_until_ms: 0,
      next_attempt_at_ms: startedAtMs + OFFSETS_MS[0],
      updated_at_ms: Number(nowMs),
    };
  }

  function normalizeJob(value = {}) {
    const offsets = Array.isArray(value.offsets_ms) && value.offsets_ms.length
      ? value.offsets_ms.map(Number).filter((item) => Number.isFinite(item) && item >= 0)
      : [...OFFSETS_MS];
    const index = Math.max(0, Math.min(offsets.length, Number(value.next_attempt_index) || 0));
    const scope = normalizedScope(value);
    const actionId = String(value.action_id || "").trim().toLowerCase();
    const identityValid = /^[a-f0-9]{24}$/.test(actionId) && scopeIsComplete(scope);
    const status = TERMINAL_STATES.has(value.status) ? value.status : identityValid ? "pending" : "uncertain";
    const startedAtMs = Math.max(1, Number(value.started_at_ms) || Date.now());
    const deferredUntilMs = status === "pending" ? Math.max(0, Number(value.deferred_until_ms) || 0) : 0;
    const scheduledAtMs = status === "pending" && index < offsets.length
      ? Math.max(startedAtMs + offsets[index], deferredUntilMs)
      : 0;
    return {
      ...value,
      schema_version: 2,
      action_id: actionId,
      ...scope,
      started_at_ms: startedAtMs,
      offsets_ms: offsets,
      next_attempt_index: index,
      attempts: Array.isArray(value.attempts) ? value.attempts.slice(-8) : [],
      status,
      status_label: identityValid
        ? String(value.status_label || (status === "pending" ? "等待平台回读" : ""))
        : "回读任务缺少店铺、账户或计划绑定；保持锁定并停止自动回读",
      deferred_until_ms: deferredUntilMs,
      next_attempt_at_ms: scheduledAtMs,
    };
  }

  function nextAttempt(jobValue = {}, nowMs = Date.now()) {
    const job = normalizeJob(jobValue);
    if (job.status !== "pending" || job.next_attempt_index >= job.offsets_ms.length) {
      return { due: false, terminal: true, wait_ms: 0, attempt_index: job.next_attempt_index };
    }
    const dueAtMs = Math.max(
      job.started_at_ms + job.offsets_ms[job.next_attempt_index],
      Number(job.deferred_until_ms || 0),
    );
    return {
      due: Number(nowMs) >= dueAtMs,
      terminal: false,
      wait_ms: Math.max(0, dueAtMs - Number(nowMs)),
      due_at_ms: dueAtMs,
      attempt_index: job.next_attempt_index,
    };
  }

  function recordAttempt(jobValue = {}, input = {}, nowMs = Date.now()) {
    const job = normalizeJob(jobValue);
    if (job.status !== "pending") return job;
    const verification = input.verification && typeof input.verification === "object" ? input.verification : null;
    const attempt = {
      index: job.next_attempt_index,
      attempted_at_ms: Number(nowMs),
      collected: input.collected === true,
      state: String(verification?.state || "unknown"),
      verified: verification?.verified === true,
      safe_to_retry: verification?.safe_to_retry === true,
      confirmation_count: Number(verification?.original_value_confirmation_count || 0),
      error: String(input.error || "").slice(0, 300),
    };
    const attempts = [...job.attempts, attempt].slice(-8);
    const nextIndex = job.next_attempt_index + 1;
    let status = "pending";
    let statusLabel = "平台尚未确认，继续锁定动作";
    if (verification?.verified === true || verification?.state === "verified") {
      status = "verified";
      statusLabel = "平台回读已确认生效";
    } else if (verification?.safe_to_retry === true && verification?.state === "failed") {
      status = "failed";
      statusLabel = "多次独立回读确认未生效，可重新生成方案";
    } else if (nextIndex >= job.offsets_ms.length) {
      status = "uncertain";
      statusLabel = "多轮回读仍未确认，保持锁定并禁止重复执行";
    }
    return {
      ...job,
      attempts,
      next_attempt_index: nextIndex,
      status,
      status_label: statusLabel,
      last_verification: verification,
      last_error: attempt.error,
      last_error_code: String(input.error_code || ""),
      deferred_until_ms: 0,
      next_attempt_at_ms: status === "pending" ? job.started_at_ms + job.offsets_ms[nextIndex] : 0,
      updated_at_ms: Number(nowMs),
    };
  }

  function deferAttempt(jobValue = {}, input = {}, nowMs = Date.now()) {
    const job = normalizeJob(jobValue);
    if (job.status !== "pending") return job;
    const delayMs = Math.max(5_000, Math.min(Number(input.delay_ms) || 60_000, 300_000));
    const deferredUntilMs = Number(nowMs) + delayMs;
    return {
      ...job,
      status_label: String(input.status_label || "等待重新打开原店铺、账户与计划页面后安全回读"),
      last_error: String(input.error || "未找到精确匹配的回读页面").slice(0, 300),
      last_error_code: String(input.error_code || "READBACK_SCOPE_TAB_UNAVAILABLE"),
      deferred_until_ms: deferredUntilMs,
      next_attempt_at_ms: deferredUntilMs,
      updated_at_ms: Number(nowMs),
    };
  }

  function reopenManualAttempt(jobValue = {}, nowMs = Date.now()) {
    const job = normalizeJob(jobValue);
    if (job.status === "verified" || job.status === "failed") return job;
    const manualAttemptCount = Math.max(0, Number(job.manual_attempt_count) || 0);
    if (manualAttemptCount >= 20) throw new Error("人工回读次数已达安全上限，请在官方后台核对后联系支持处理锁定动作");
    if (job.status === "pending") {
      const offsets = [...job.offsets_ms];
      offsets[job.next_attempt_index] = Math.max(0, Number(nowMs) - Number(job.started_at_ms || nowMs));
      return normalizeJob({
        ...job,
        offsets_ms: offsets,
        deferred_until_ms: 0,
        status_label: "用户已请求立即执行一次独立回读",
        manual_attempt_count: manualAttemptCount + 1,
        updated_at_ms: Number(nowMs),
      });
    }
    const dueOffset = Math.max(0, Number(nowMs) - Number(job.started_at_ms || nowMs));
    return normalizeJob({
      ...job,
      offsets_ms: [...job.offsets_ms.slice(0, job.next_attempt_index), dueOffset],
      status: "pending",
      status_label: "用户已请求追加一次独立页面回读",
      manual_attempt_count: manualAttemptCount + 1,
      deferred_until_ms: 0,
      updated_at_ms: Number(nowMs),
    });
  }

  function summarize(jobsValue = {}) {
    const jobs = Object.values(jobsValue || {}).map(normalizeJob);
    const pending = jobs.filter((item) => item.status === "pending");
    const uncertain = jobs.filter((item) => item.status === "uncertain");
    const latest = jobs.sort((a, b) => Number(b.updated_at_ms || 0) - Number(a.updated_at_ms || 0))[0] || null;
    return {
      pending_count: pending.length,
      uncertain_count: uncertain.length,
      action_locked: Boolean(pending.length || uncertain.length),
      latest,
    };
  }

  return { OFFSETS_MS, createJob, normalizeJob, nextAttempt, recordAttempt, deferAttempt, reopenManualAttempt, summarize };
});
