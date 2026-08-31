/** 店策 Agent - 千川多账号巡检策略 */
(function () {
  "use strict";

  function matchAccount(captured, expected) {
    if (!expected?.key) return { ok: true, matchedBy: "auto" };
    if (!captured?.key) {
      return {
        ok: false,
        code: "ACCOUNT_UNRESOLVED",
        message: "当前千川页面暂时不能用于本次任务。请打开要查看的千川账户后重试。",
      };
    }
    if (captured.key === expected.key) return { ok: true, matchedBy: "key" };

    return {
      ok: false,
      code: "ACCOUNT_MISMATCH",
        message: "检测到千川账户已切换，本次巡检已暂停。请回到要查看的账户后重试。",
    };
  }

  const SCAN_RECOVERY_SCHEMA_VERSION = 1;

  const RECOVERY_RULES = Object.freeze({
    SCAN_CANCELLED: { state: "cancelled", action: "start_new_scan", message: "本轮巡店已由用户停止。", canResume: false },
    LOGIN_REQUIRED: { state: "blocked_login", action: "login_then_resume", message: "登录状态已失效，请登录对应平台后续跑。" },
    ACCOUNT_IDENTITY_CONFLICT: { state: "blocked_identity", action: "confirm_account_then_resume", message: "检测到多个千川账户，本次已暂停。请打开要查看的账户后重试。" },
    ACCOUNT_UNRESOLVED: { state: "blocked_identity", action: "confirm_account_then_resume", message: "当前千川页面暂时不能用于本次任务，请打开要查看的账户后重试。" },
    ACCOUNT_MISMATCH: { state: "blocked_identity", action: "switch_account_then_resume", message: "检测到千川账户已切换，本次已暂停。请回到要查看的账户后重试。" },
    STORE_IDENTITY_CONFLICT: { state: "blocked_identity", action: "confirm_store_then_resume", message: "检测到店铺正在切换，本次已暂停。请回到要经营的抖店首页后重试。" },
    STORE_IDENTITY_UNRESOLVED: { state: "blocked_identity", action: "confirm_store_then_resume", message: "这个页面暂时不能用于经营判断，请打开抖店首页后重试。" },
    STORE_MISMATCH: { state: "blocked_identity", action: "switch_store_then_resume", message: "检测到店铺已切换，本次已暂停。请回到要经营的抖店首页后重试。" },
    STORE_SELECTION_INVALID: { state: "blocked_identity", action: "select_store_then_resume", message: "当前经营页面已经失效，请重新打开抖店首页。" },
    STORE_SELECTION_CONFLICT: { state: "blocked_identity", action: "select_store_then_resume", message: "当前页面范围不一致，本次已暂停。请重新打开抖店首页。" },
    STORE_SELECTION_UNCONFIRMED: { state: "blocked_identity", action: "select_store_then_resume", message: "尚未打开可用的抖店页面，请打开抖店首页后重试。" },
    STORE_SELECTION_MISMATCH: { state: "blocked_identity", action: "select_store_then_resume", message: "检测到店铺已切换，请回到要经营的抖店首页。" },
    PAGE_SOURCE_MISMATCH: { state: "blocked_page", action: "open_expected_page_then_resume", message: "页面被导航到其他平台或非预期站点，请打开正确页面后续跑。" },
    PAGE_TYPE_MISMATCH: { state: "blocked_page", action: "open_expected_page_then_resume", message: "页面内容与巡店项目不匹配，请确认页签完成切换后续跑。" },
    PLAN_TABLE_UNRECOGNIZED: { state: "blocked_page", action: "open_expected_page_then_resume", message: "未识别计划表，请确认计划页已完整加载后续跑。" },
    PLAN_IDENTIFIERS_UNRESOLVED: { state: "blocked_evidence", action: "refresh_or_narrow_then_resume", message: "计划身份覆盖不足，不能作为完整证据；请刷新或缩小筛选范围后续跑。" },
    COLLECTION_TRUNCATED: { state: "partial_collection", action: "narrow_filters_then_resume", message: "分页或虚拟列表未读取完整，请缩小日期或筛选范围后续跑。" },
    COLLECTION_STALLED: { state: "retryable", action: "refresh_then_resume", message: "列表翻页或虚拟滚动没有继续加载，请刷新页面后续跑。" },
    COLLECTION_CONTEXT_CHANGED: { state: "retryable", action: "reopen_page_then_resume", message: "采集期间店铺、账户或页面范围发生变化；本次候选已丢弃，请回到正确页面后续跑。" },
    PAGE_LOAD_TIMEOUT: { state: "retryable", action: "check_network_then_resume", message: "页面加载超时，请检查网络后续跑。" },
    PAGE_NOT_READY: { state: "retryable", action: "wait_then_resume", message: "页面仍在加载，请稍后续跑。" },
    TAB_CLOSED: { state: "retryable", action: "resume_failed_pages", message: "巡店临时页面已关闭，可从失败页继续。" },
    CONTENT_SCRIPT_UNAVAILABLE: { state: "retryable", action: "reload_extension_then_resume", message: "页面采集器未就绪，请重新加载扩展后续跑。" },
    PLAN_TABLE_LOADING: { state: "retryable", action: "wait_then_resume", message: "计划表仍在加载，请稍后续跑。" },
    PLATFORM_PAGE_UNAVAILABLE: { state: "retryable", action: "check_network_then_resume", message: "平台页面暂不可用，请检查网络或稍后续跑。" },
    SCAN_INTERRUPTED: { state: "paused_interrupted", action: "resume_failed_pages", message: "浏览器中断了巡店，断点已保留；请手动续跑。" },
  });

  function errorCode(error) {
    if (error?.code) return String(error.code);
    const message = String(error?.message || error || "");
    const structured = message.match(/\b(SCAN_CANCELLED|SCAN_INTERRUPTED|STORE_SELECTION_INVALID|STORE_SELECTION_CONFLICT|STORE_SELECTION_UNCONFIRMED|STORE_SELECTION_MISMATCH|STORE_IDENTITY_CONFLICT|STORE_IDENTITY_UNRESOLVED|STORE_MISMATCH|ACCOUNT_IDENTITY_CONFLICT|ACCOUNT_UNRESOLVED|ACCOUNT_MISMATCH|LOGIN_REQUIRED|PAGE_SOURCE_MISMATCH|PAGE_TYPE_MISMATCH|PAGE_LOAD_TIMEOUT|PAGE_NOT_READY|TAB_CLOSED|CONTENT_SCRIPT_UNAVAILABLE|PLATFORM_PAGE_UNAVAILABLE|COLLECTION_CONTEXT_CHANGED|COLLECTION_STALLED|COLLECTION_TRUNCATED|PLAN_TABLE_UNRECOGNIZED|PLAN_TABLE_LOADING|PLAN_IDENTIFIERS_UNRESOLVED)\b/);
    if (structured) return structured[1];
    if (/未识别当前千川账号/.test(message)) return "ACCOUNT_UNRESOLVED";
    if (/与本轮锁定账号不一致|与所选巡查账号不一致/.test(message)) return "ACCOUNT_MISMATCH";
    if (/所属店铺与已选店铺不一致/.test(message)) return "STORE_MISMATCH";
    if (/没有可靠店铺身份|尚未识别当前店铺/.test(message)) return "STORE_IDENTITY_UNRESOLVED";
    if (/登录已失效|完成登录/.test(message)) return "LOGIN_REQUIRED";
    if (/页面识别为.+预期为/.test(message)) return "PAGE_TYPE_MISMATCH";
    if (/页面加载超时|加载超过.+秒/.test(message)) return "PAGE_LOAD_TIMEOUT";
    if (/巡店页面已关闭|No tab with id|Invalid tab ID/i.test(message)) return "TAB_CLOSED";
    if (/翻页后内容没有更新|虚拟列表.+未读取完整/.test(message)) return "COLLECTION_STALLED";
    return "";
  }

  function isNonRetryable(error) {
    return [
      "ACCOUNT_UNRESOLVED", "ACCOUNT_IDENTITY_CONFLICT", "ACCOUNT_MISMATCH",
      "STORE_MISMATCH", "STORE_IDENTITY_UNRESOLVED", "STORE_IDENTITY_CONFLICT",
      "LOGIN_REQUIRED", "SCAN_CANCELLED",
    ].includes(errorCode(error));
  }

  function shouldStopAfterResult(result) {
    if (!result || result.ok !== false) return false;
    return [
      "ACCOUNT_UNRESOLVED", "ACCOUNT_IDENTITY_CONFLICT", "ACCOUNT_MISMATCH",
      "STORE_MISMATCH", "STORE_IDENTITY_UNRESOLVED", "STORE_IDENTITY_CONFLICT",
      "LOGIN_REQUIRED", "SCAN_CANCELLED",
    ].includes(String(result.error_code || ""));
  }

  function resumePageIds(scan = {}, fallbackPlannedPageIds = []) {
    const planned = Array.isArray(scan.planned_page_ids) && scan.planned_page_ids.length
      ? scan.planned_page_ids
      : Array.isArray(scan.targeted_page_ids) && scan.targeted_page_ids.length
        ? scan.targeted_page_ids
        : fallbackPlannedPageIds;
    const attempted = new Set(
      Array.isArray(scan.attempted_page_ids)
        ? scan.attempted_page_ids
        : (scan.results || []).map((item) => item?.id).filter(Boolean),
    );
    const pending = Array.isArray(scan.pending_page_ids) && scan.pending_page_ids.length
      ? scan.pending_page_ids
      : planned.filter((id) => !attempted.has(id));
    const failed = (scan.results || [])
      .filter((item) => item?.id && (item.ok === false || item.collection_complete === false))
      .filter((item) => String(item.error_code || item.warning_code || "") !== "SCAN_CANCELLED")
      .map((item) => item.id);
    const allowed = new Set(planned);
    return [...new Set([...failed, ...pending])].filter((id) => allowed.has(id));
  }

  function recoveryDirective(errorOrCode, options = {}) {
    const code = typeof errorOrCode === "string" ? errorOrCode : errorCode(errorOrCode);
    const rule = RECOVERY_RULES[code] || {
      state: options.interrupted ? "paused_interrupted" : "needs_review",
      action: options.interrupted ? "resume_failed_pages" : "review_then_resume",
      message: options.interrupted ? RECOVERY_RULES.SCAN_INTERRUPTED.message : "巡店失败原因尚未归类，请检查页面后手动续跑。",
    };
    return {
      schema_version: SCAN_RECOVERY_SCHEMA_VERSION,
      state: rule.state,
      error_code: code || (options.interrupted ? "SCAN_INTERRUPTED" : "UNKNOWN_SCAN_ERROR"),
      action: rule.action,
      message: rule.message,
      automatic_resume: false,
      requires_user_action: !["complete", "running"].includes(rule.state),
      can_resume: rule.canResume !== false,
    };
  }

  function buildRecoveryCheckpoint(scan = {}, fallbackPlannedPageIds = []) {
    const allResults = Array.isArray(scan.results) ? scan.results : [];
    const planned = Array.isArray(scan.planned_page_ids) && scan.planned_page_ids.length
      ? scan.planned_page_ids
      : fallbackPlannedPageIds;
    const allowed = new Set((planned || []).map((item) => String(item || "")).filter(Boolean));
    // A targeted refresh can retain successful historical results for the
    // overall dashboard. Recovery decisions, however, belong only to this
    // run's locked page contract. An unrelated old failure must not turn a
    // one-page refresh into a blocked/partial recovery flow.
    const results = allowed.size
      ? allResults.filter((item) => item?.id && allowed.has(String(item.id)))
      : allResults;
    const resumeIds = resumePageIds(scan, fallbackPlannedPageIds);
    const failed = results.filter((item) => item?.ok === false);
    const incomplete = results.filter((item) => item?.ok !== false && item?.collection_complete === false);
    const blocking = failed.find((item) => shouldStopAfterResult(item)) || failed[0] || incomplete[0] || null;
    if (scan.status === "completed" && !resumeIds.length && !incomplete.length) {
      return {
        schema_version: SCAN_RECOVERY_SCHEMA_VERSION,
        state: "complete",
        error_code: "",
        action: "none",
        message: "本轮巡店已完成。",
        automatic_resume: false,
        requires_user_action: false,
        can_resume: false,
        resume_page_ids: [],
      };
    }
    if (scan.status === "cancelled") {
      return { ...recoveryDirective("SCAN_CANCELLED"), resume_page_ids: [] };
    }
    if (scan.status === "running" && !blocking && !scan.interrupted) {
      return {
        schema_version: SCAN_RECOVERY_SCHEMA_VERSION,
        state: "running",
        error_code: "",
        action: "none",
        message: "巡店正在运行。",
        automatic_resume: false,
        requires_user_action: false,
        can_resume: false,
        resume_page_ids: resumeIds,
      };
    }
    const code = String(blocking?.error_code || blocking?.warning_code || scan.error_code || (incomplete.length ? "COLLECTION_TRUNCATED" : ""));
    const directive = recoveryDirective(code, { interrupted: scan.status === "interrupted" || scan.interrupted === true });
    return { ...directive, resume_page_ids: directive.can_resume ? resumeIds : [] };
  }

  function canCancelScan(scan = {}) {
    return Boolean(String(scan.run_id || "") && ["running", "interrupted"].includes(String(scan.status || "")));
  }

  function acceptScanStatusTransition(currentStatus = "", nextStatus = "", options = {}) {
    const current = String(currentStatus || "");
    const next = String(nextStatus || "");
    if (!next || current === next) return true;
    const immutableTerminal = ["completed", "partial", "cancelled", "error"];
    if (immutableTerminal.includes(current)) return false;
    if (options.cancelRequested === true && ["completed", "partial", "error"].includes(next)) return false;
    return true;
  }

  function inspectPlanIdentityCoverage(snapshot = {}, pageType = "") {
    const normalizedPageType = String(pageType || snapshot?.page_type || "").trim().toLowerCase();
    if (!["campaigns", "qianchuan_campaigns", "qianchuan_live", "plans"].includes(normalizedPageType)) {
      return { complete: true, applicable: false, code: "", message: "" };
    }
    const coverage = snapshot?.quality?.plan_identity_coverage;
    if (!coverage || typeof coverage !== "object") {
      // The localhost bridge remains the authoritative structural gate and
      // rejects missing/loading plan tables. Do not invent browser evidence.
      return { complete: true, applicable: true, code: "", message: "" };
    }
    const eligibleRows = Math.max(0, Number(coverage.eligible_rows) || 0);
    const identifiedRows = Math.max(0, Number(coverage.identified_rows) || 0);
    if (eligibleRows > identifiedRows) {
      return {
        complete: false,
        applicable: true,
        code: "PLAN_IDENTIFIERS_UNRESOLVED",
        message: `计划身份仅识别 ${identifiedRows}/${eligibleRows} 行，缺少稳定计划 ID 的行已隔离；请刷新或缩小筛选范围后续跑。`,
      };
    }
    return { complete: true, applicable: true, code: "", message: "" };
  }

  function inspectPageAccess(page = {}, source = "") {
    const href = String(page.href || page.url || "");
    const title = String(page.title || "");
    const readyState = String(page.readyState || "");
    const loginEvidence = /(?:^|[/.?=&_-])login(?:[/?#=&_-]|$)/i.test(href)
      || /passport/i.test(href)
      || /扫码登录|验证码登录|登录(?:\s*[-|_]|$)/i.test(title);
    if (loginEvidence) {
      return { ok: false, code: "LOGIN_REQUIRED", message: `${source === "qianchuan" ? "千川" : "抖店"}登录已失效，请完成登录后手动续跑。` };
    }
    const sourceMatches = source === "doudian"
      ? /^https:\/\/fxg\.jinritemai\.com\//i.test(href)
      : source === "qianchuan"
        ? /^https:\/\/(?:qianchuan|buyin)\.jinritemai\.com\//i.test(href)
        : false;
    if (!sourceMatches) return { ok: false, code: "PAGE_SOURCE_MISMATCH", message: "页面已离开预期平台，巡店未读取也未合并该页。" };
    if (/(?:502|503|504|服务不可用|网络错误|页面崩溃|无法访问)/i.test(title)) {
      return { ok: false, code: "PLATFORM_PAGE_UNAVAILABLE", message: "平台页面暂不可用，请检查网络后续跑。" };
    }
    if (readyState && !["interactive", "complete"].includes(readyState)) {
      return { ok: false, code: "PAGE_NOT_READY", message: "页面仍在加载，尚未进入可采集状态。" };
    }
    return { ok: true, code: "", message: "" };
  }

  function mergeScopedResults(previousScan = {}, attemptedResults = [], scope = {}, pageOrder = []) {
    const sameStore = String(previousScan.store_key || "") === String(scope.storeKey || "");
    const sameAccount = String(previousScan.account_key || "") === String(scope.accountKey || "");
    if (!sameStore || !sameAccount) return [...attemptedResults];
    const merged = new Map((previousScan.results || []).filter((item) => item?.id).map((item) => [item.id, item]));
    attemptedResults.filter((item) => item?.id).forEach((item) => merged.set(item.id, item));
    const order = pageOrder.length ? pageOrder : [...merged.keys()];
    return order.map((id) => merged.get(id)).filter(Boolean);
  }

  function reconcileRecoveryProgress(previousScan = {}, attemptedResults = [], scope = {}, fallbackPlannedPageIds = []) {
    const planned = [...new Set(
      (Array.isArray(previousScan.planned_page_ids) && previousScan.planned_page_ids.length
        ? previousScan.planned_page_ids
        : fallbackPlannedPageIds)
        .map((value) => String(value || "").trim())
        .filter(Boolean),
    )];
    const results = mergeScopedResults(previousScan, attemptedResults, scope, planned);
    const byId = new Map(results.filter((item) => item?.id).map((item) => [String(item.id), item]));
    const attemptedPageIds = planned.filter((id) => byId.has(id));
    const pendingPageIds = planned.filter((id) => {
      const result = byId.get(id);
      return !result || result.ok === false || result.collection_complete === false;
    });
    const scopedResults = planned.map((id) => byId.get(id)).filter(Boolean);
    return {
      root_run_id: String(previousScan.root_run_id || previousScan.run_id || ""),
      planned_page_ids: planned,
      results: scopedResults,
      attempted_page_ids: attemptedPageIds,
      pending_page_ids: pendingPageIds,
      coverage_complete: planned.length > 0 && pendingPageIds.length === 0,
      success: scopedResults.filter((item) => item?.ok !== false && item?.collection_complete !== false).length,
      failed: scopedResults.filter((item) => item?.ok === false).length,
      low_quality: scopedResults.filter((item) => item?.ok !== false && Number(item?.quality?.score || 0) < 50).length,
    };
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

  function isQianchuanUrl(value) {
    const url = String(value || "");
    return url.startsWith("https://qianchuan.jinritemai.com/")
      || url.startsWith("https://buyin.jinritemai.com/");
  }

  function selectQianchuanSyncTab(tabs, activeTab = null, recentTabId = null, seedTabId = null) {
    if (activeTab?.id && isQianchuanUrl(activeTab.url)) {
      return { tab: activeTab, matchedBy: "active" };
    }
    const candidates = (tabs || []).filter((tab) => tab?.id && isQianchuanUrl(tab.url));
    if (!candidates.length) return { tab: null, matchedBy: "none" };
    const preferredTabId = candidates.some((tab) => tab.id === recentTabId)
      ? recentTabId
      : candidates.some((tab) => tab.id === seedTabId) ? seedTabId : null;
    const [tab] = rankSeedTabs(candidates, preferredTabId);
    return {
      tab: tab || null,
      matchedBy: tab?.id === recentTabId ? "recent" : tab?.id === seedTabId ? "seed" : "latest",
    };
  }

  function shouldAcceptScanSnapshot(currentValue = {}, incomingValue = {}) {
    const current = currentValue && typeof currentValue === "object" ? currentValue : {};
    const incoming = incomingValue && typeof incomingValue === "object" ? incomingValue : {};
    const currentRun = String(current.run_id || "");
    const incomingRun = String(incoming.run_id || "");
    const currentRevision = Number(current.revision);
    const incomingRevision = Number(incoming.revision);
    const currentHasRevision = Number.isInteger(currentRevision) && currentRevision > 0;
    const incomingHasRevision = Number.isInteger(incomingRevision) && incomingRevision > 0;

    if (currentHasRevision && incomingHasRevision) {
      if (incomingRevision < currentRevision) return false;
      if (incomingRevision === currentRevision && currentRun && incomingRun && currentRun !== incomingRun) return false;
      return true;
    }
    if (currentHasRevision && !incomingHasRevision) return false;

    const currentStartedAt = Number(current.started_at || current.root_started_at || 0);
    const incomingStartedAt = Number(incoming.started_at || incoming.root_started_at || 0);
    if (currentRun && incomingRun && currentRun !== incomingRun) {
      // Legacy snapshots without revisions may only replace another run when
      // their start time proves that they are newer.
      return incomingStartedAt > 0 && currentStartedAt > 0 && incomingStartedAt >= currentStartedAt;
    }
    const currentHeartbeat = Number(current.heartbeat_at || current.finished_at || currentStartedAt || 0);
    const incomingHeartbeat = Number(incoming.heartbeat_at || incoming.finished_at || incomingStartedAt || 0);
    return !currentHeartbeat || !incomingHeartbeat || incomingHeartbeat >= currentHeartbeat;
  }

  function normalizeStoreKey(value) {
    const key = String(value || "").trim().toLowerCase();
    return /^[a-z0-9_-]{1,48}$/.test(key) ? key : "";
  }

  function confirmSelectedStore(catalog = {}, requestedStoreKey = "") {
    const stores = Array.isArray(catalog?.stores) ? catalog.stores : [];
    const knownStoreKeys = new Set(stores.map((item) => normalizeStoreKey(item?.key)).filter(Boolean));
    const selectedStoreKey = normalizeStoreKey(catalog?.selected_store_key);
    const requestedRaw = String(requestedStoreKey || "").trim();
    const requestedNormalized = normalizeStoreKey(requestedRaw);
    if (requestedRaw && !requestedNormalized) {
      return { ok: false, code: "STORE_SELECTION_INVALID", message: "请求巡检的店铺标识无效，已停止巡检。" };
    }
    const flaggedSelectedKeys = [...new Set(
      stores.filter((item) => item?.selected === true).map((item) => normalizeStoreKey(item?.key)).filter(Boolean),
    )];
    if (flaggedSelectedKeys.length > 1) {
      return { ok: false, code: "STORE_SELECTION_CONFLICT", message: "店铺目录存在多个已选店铺，已停止巡检。" };
    }
    if (selectedStoreKey && flaggedSelectedKeys[0] && selectedStoreKey !== flaggedSelectedKeys[0]) {
      return { ok: false, code: "STORE_SELECTION_CONFLICT", message: "店铺目录的选择状态不一致，已停止巡检。" };
    }
    const confirmedStoreKey = selectedStoreKey || flaggedSelectedKeys[0] || "";
    const requested = requestedNormalized || confirmedStoreKey;
    if (!confirmedStoreKey || !requested || !knownStoreKeys.has(confirmedStoreKey)) {
      return { ok: false, code: "STORE_SELECTION_UNCONFIRMED", message: "当前店铺尚未在本地目录中确认，已停止巡检。" };
    }
    if (requested !== confirmedStoreKey || !knownStoreKeys.has(requested)) {
      return { ok: false, code: "STORE_SELECTION_MISMATCH", message: "请求巡检的店铺与当前已选店铺不一致，已停止巡检。" };
    }
    return { ok: true, storeKey: confirmedStoreKey };
  }

  function inspectDoudianCurrentIdentityProof(snapshot = {}) {
    if (!snapshot || typeof snapshot !== "object") {
      return { ok: false, code: "STORE_IDENTITY_UNRESOLVED", reason: "invalid_snapshot" };
    }
    const identityStatus = String(snapshot.identity_status || "");
    if (identityStatus === "conflict") {
      return { ok: false, code: "STORE_IDENTITY_CONFLICT", reason: "identity_conflict" };
    }
    if (identityStatus !== "resolved_by_bridge") {
      return { ok: false, code: "STORE_IDENTITY_UNRESOLVED", reason: "identity_status_unresolved" };
    }
    const pageType = String(snapshot.page_type || "");
    const allowedSources = new Set(["url_parameter", "data_attribute", "overview_visible_text", "allowlisted_storage", "bootstrap_sec_shop_id"]);
    const claims = (Array.isArray(snapshot.identity_claims) ? snapshot.identity_claims : [])
      .filter((claim) => claim?.kind === "douyin_shop_id")
      .filter((claim) => /^[A-Za-z0-9_-]{4,80}$/.test(String(claim?.raw_id || "")))
      .filter((claim) => allowedSources.has(String(claim?.evidence_source || "")))
      .filter((claim) => String(claim?.evidence_source || "") !== "overview_visible_text" || pageType === "overview")
      .filter((claim) => ["high", "medium"].includes(String(claim?.confidence || "")));
    const distinctIds = [...new Set(claims.map((claim) => String(claim.raw_id)))];
    if (distinctIds.length !== 1) {
      return {
        ok: false,
        code: distinctIds.length > 1 ? "STORE_IDENTITY_CONFLICT" : "STORE_IDENTITY_UNRESOLVED",
        reason: distinctIds.length > 1 ? "multiple_real_identity_claims" : "real_identity_claim_required",
      };
    }
    const claim = claims.find((item) => String(item.raw_id) === distinctIds[0]);
    return {
      ok: true,
      code: "",
      reason: "verified_current_claim",
      claim: {
        kind: "douyin_shop_id",
        raw_id: distinctIds[0],
        confidence: String(claim.confidence),
        evidence_source: String(claim.evidence_source),
      },
    };
  }

  function inspectDoudianIdentityProof(snapshot = {}, options = {}) {
    if (!snapshot || typeof snapshot !== "object") {
      return inspectDoudianCurrentIdentityProof(snapshot);
    }
    if (String(options.pageType || snapshot.page_type || "") !== "overview") {
      return { ok: false, code: "STORE_IDENTITY_UNRESOLVED", reason: "bootstrap_page_required" };
    }
    const proof = inspectDoudianCurrentIdentityProof(snapshot);
    return proof.ok ? { ...proof, reason: "verified_overview_claim" } : proof;
  }

  function createDoudianScanContext(storeKey, options = {}) {
    const normalizedStoreKey = normalizeStoreKey(storeKey);
    if (!normalizedStoreKey) return null;
    const now = Number(options.now ?? Date.now());
    const safeTtlMs = Math.max(5000, Math.min(Number(options.ttlMs) || 900000, 900000));
    const runId = String(options.runId || "");
    const tabId = Number(options.tabId);
    const proof = options.proof && typeof options.proof === "object" ? options.proof : {};
    if (!runId || !Number.isInteger(tabId)) return null;
    if (
      proof.verified !== true
      || normalizeStoreKey(proof.store_key) !== normalizedStoreKey
      || String(proof.page_type || "") !== "overview"
      || String(proof.identity_source || "") !== "hmac_douyin_shop_id"
    ) return null;
    return {
      source: "doudian",
      store_key: normalizedStoreKey,
      run_id: runId,
      tab_id: tabId,
      created_at: now,
      expires_at: now + safeTtlMs,
      proof: {
        page_type: "overview",
        identity_source: "hmac_douyin_shop_id",
        verified_at: Number(proof.verified_at || now),
      },
    };
  }

  function routePatternMatches(pattern, value) {
    if (!(pattern instanceof RegExp)) return false;
    pattern.lastIndex = 0;
    return pattern.test(String(value || ""));
  }

  function resolveQianchuanRoutes(current = {}, discoveredLinks = []) {
    const currentUrl = String(current?.url || "");
    const links = (Array.isArray(discoveredLinks) ? discoveredLinks : [])
      .filter((item) => item && typeof item === "object" && isQianchuanUrl(item.url));
    const pick = (textPattern, urlPattern = null, options = {}) => {
      const textMatch = links.find((item) => routePatternMatches(textPattern, item.text));
      if (textMatch) return String(textMatch.url || "");

      const urlMatch = urlPattern && links.find((item) => (
        String(item.url || "") !== currentUrl
        && routePatternMatches(urlPattern, item.url)
      ));
      if (urlMatch) return String(urlMatch.url || "");
      if (options.allowCurrentUrl && routePatternMatches(urlPattern, currentUrl)) return currentUrl;
      return "";
    };
    return {
      qianchuan_overview: pick(/(?:经营|数据)?概览|首页/, /\/(?:home|overview)(?:[/?#]|$)/i, { allowCurrentUrl: true }),
      qianchuan_campaigns: pick(/商品(?:全域)?推广|推广管理/, /\/(?:brand_bid\/promotion\/standard|uni-prom\/(?:product|campaign|standard))(?:[/?#]|$)/i),
      qianchuan_live: pick(/直播(?:全域|间)?推广/, /\/(?:uni-prom\/live|live[^/?#]*\/(?:prom|campaign)|(?:prom|campaign)[^/?#]*\/live)(?:[/?#]|$)/i),
      qianchuan_live_dashboard: pick(/直播大屏/, /\/(?:board(?:-next)?|live-dashboard)(?:[/?#]|$)/i, { allowCurrentUrl: true }),
      qianchuan_video_library: pick(/视频库|素材分析|视频素材/, /\/(?:dataV2\/roi2-material-analysis|video-library|material-analysis)(?:[/?#]|$)/i, { allowCurrentUrl: true }),
    };
  }

  function applyDoudianScanContext(snapshot, context, options = {}) {
    const source = String(options.source || "");
    const tabUrl = String(options.tabUrl || "");
    const now = Number(options.now ?? Date.now());
    if (!snapshot || typeof snapshot !== "object") return { snapshot, applied: false, reason: "invalid_snapshot" };
    if (source !== "doudian" || context?.source !== "doudian") return { snapshot, applied: false, reason: "wrong_source" };
    if (!tabUrl.startsWith("https://fxg.jinritemai.com/")) return { snapshot, applied: false, reason: "wrong_origin" };
    if (!String(options.runId || "") || String(options.runId) !== String(context?.run_id || "")) {
      return { snapshot, applied: false, reason: "run_mismatch" };
    }
    if (!Number.isInteger(Number(options.tabId)) || Number(options.tabId) !== Number(context?.tab_id)) {
      return { snapshot, applied: false, reason: "tab_mismatch" };
    }
    if (!normalizeStoreKey(context?.store_key) || now >= Number(context?.expires_at || 0)) {
      return { snapshot, applied: false, reason: "expired" };
    }
    if (
      context?.proof?.page_type !== "overview"
      || context?.proof?.identity_source !== "hmac_douyin_shop_id"
    ) return { snapshot, applied: false, reason: "unverified_lease" };
    const currentProof = inspectDoudianCurrentIdentityProof(snapshot);
    if (!currentProof.ok) {
      return {
        snapshot,
        applied: false,
        reason: currentProof.code === "STORE_IDENTITY_CONFLICT" ? "identity_conflict" : "fresh_identity_required",
        code: currentProof.code,
      };
    }
    return {
      snapshot,
      applied: false,
      reason: "fresh_identity_claim",
      proof: currentProof.claim,
    };
  }

  globalThis.DianAgentScanPolicy = {
    matchAccount,
    errorCode,
    isNonRetryable,
    rankSeedTabs,
    shouldAcceptScanSnapshot,
    isQianchuanUrl,
    selectQianchuanSyncTab,
    resolveQianchuanRoutes,
    normalizeStoreKey,
    confirmSelectedStore,
    createDoudianScanContext,
    inspectDoudianCurrentIdentityProof,
    inspectDoudianIdentityProof,
    applyDoudianScanContext,
    shouldStopAfterResult,
    resumePageIds,
    mergeScopedResults,
    reconcileRecoveryProgress,
    recoveryDirective,
    buildRecoveryCheckpoint,
    canCancelScan,
    acceptScanStatusTransition,
    inspectPlanIdentityCoverage,
    inspectPageAccess,
  };
})();
