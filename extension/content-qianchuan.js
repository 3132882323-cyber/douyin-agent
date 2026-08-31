/** 巨量千川页面采集器 */
(function () {
  "use strict";
  if (globalThis.__DianAgentQianchuanLoaded) return;
  globalThis.__DianAgentQianchuanLoaded = true;
  const SOURCE = "qianchuan";
  const RUNTIME_VERSION = String(chrome.runtime.getManifest?.().version || "");
  globalThis.__DianAgentQianchuanRuntimeVersion = RUNTIME_VERSION;
  const RENDER_DELAY = 3200;
  const DOCUMENT_INSTANCE_ID = (() => {
    try {
      if (typeof globalThis.crypto?.randomUUID === "function") {
        return globalThis.crypto.randomUUID();
      }
      if (typeof globalThis.crypto?.getRandomValues === "function") {
        const bytes = new Uint8Array(16);
        globalThis.crypto.getRandomValues(bytes);
        return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
      }
    } catch {
      // The non-cryptographic fallback is only a per-document correlation ID.
    }
    return `doc-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
  })();
  const NAVIGATION_STARTED_AT_MS = (() => {
    const timeOrigin = Number(globalThis.performance?.timeOrigin);
    return Number.isFinite(timeOrigin) && timeOrigin > 0 ? Math.trunc(timeOrigin) : Date.now();
  })();
  const consumedExecutionAttemptIds = new Set();
  let collectionContextEpoch = 0;
  let lastCollectionContextFingerprint = "";
  let contextObserverStarted = false;
  let lastUrl = location.href;
  let routeTimer = null;

  function storedIdentityId(keys, jsonKeys) {
    const keyPattern = new RegExp(`^(?:${keys.join("|")})$`, "i");
    const jsonPattern = new RegExp(`"(?:${jsonKeys.join("|")})"\\s*:\\s*"?([A-Za-z0-9_-]{4,80})"?`);
    const found = [];
    for (const storage of [globalThis.sessionStorage, globalThis.localStorage]) {
      if (!storage) continue;
      try {
        for (let index = 0; index < Math.min(storage.length, 120); index += 1) {
          const key = String(storage.key(index) || "");
          const value = String(storage.getItem(key) || "").slice(0, 4096);
          if (keyPattern.test(key) && /^[A-Za-z0-9_-]{4,80}$/.test(value)) found.push(value);
          const match = value.match(jsonPattern);
          if (match?.[1]) found.push(match[1]);
        }
      } catch {
        // Storage evidence is optional and never treated as high confidence.
      }
    }
    return [...new Set(found)];
  }

  function identityClaim(kind, parameterKeys, attributeNames, storedKeys) {
    const searchParams = new URLSearchParams(location.search || "");
    const hashSearch = String(location.hash || "").includes("?")
      ? String(location.hash).slice(String(location.hash).indexOf("?"))
      : "";
    const hashParams = new URLSearchParams(hashSearch);
    const queryIds = parameterKeys.flatMap((key) => [searchParams.get(key), hashParams.get(key)])
      .filter((value) => value && /^[A-Za-z0-9_-]{4,80}$/.test(value));
    const selector = attributeNames.map((name) => `[${name}]`).join(", ");
    const attributeIds = selector ? Array.from(document.querySelectorAll(selector))
      .flatMap((element) => attributeNames.map((name) => element.getAttribute(name)))
      .filter((value) => value && /^[A-Za-z0-9_-]{4,80}$/.test(value)) : [];
    const highCandidates = [...new Set([...queryIds, ...attributeIds])];
    if (highCandidates.length > 1) return { conflict: true, kind };
    if (highCandidates.length === 1) {
      return {
        kind,
        raw_id: highCandidates[0],
        evidence_source: queryIds.includes(highCandidates[0]) ? "url_parameter" : "data_attribute",
        confidence: "high",
      };
    }
    const storedIds = storedIdentityId(storedKeys, parameterKeys);
    if (storedIds.length > 1) return { conflict: true, kind };
    return storedIds.length === 1 ? { kind, raw_id: storedIds[0], evidence_source: "allowlisted_storage", confidence: "medium" } : null;
  }

  function detectIdentityClaims() {
    if (location.pathname === "/login" || location.pathname.startsWith("/login/")) return { claims: [], status: "unresolved" };
    const store = identityClaim("douyin_shop_id", ["shop_id", "shopId", "store_id", "storeId"], ["data-shop-id", "data-store-id"], ["shop_id", "shopId", "store_id", "storeId", "selected_shop_id"]);
    // Current Qianchuan and Chengfang routes use `aavid` for the advertiser.
    // Keep that semantic distinction: OAuth account_id may represent the
    // parent shop account while this value identifies the active ad account.
    const advertiser = identityClaim("qianchuan_advertiser_id", ["aavid", "advertiser_id", "advertiserId", "aadvid", "advid", "adv_id"], ["data-aavid", "data-advertiser-id", "data-aadvid"], ["aavid", "advertiser_id", "advertiserId", "aadvid", "advid", "adv_id", "selected_advertiser_id"]);
    const account = advertiser?.raw_id ? null : identityClaim("qianchuan_account_id", ["account_id", "accountId"], ["data-account-id"], ["account_id", "accountId", "selected_account_id"]);
    const values = [store, advertiser, account].filter(Boolean);
    return {
      claims: values.filter((item) => item.raw_id),
      conflicts: values.filter((item) => item.conflict).map((item) => item.kind).filter(Boolean),
      status: values.some((item) => item.conflict) ? "conflict" : values.some((item) => item.raw_id) ? "resolved_by_bridge" : "unresolved",
    };
  }

  const ACTIVE_NAV_SELECTOR = [
    "[role='tab'][aria-selected='true']",
    "[role='menuitem'][aria-current='page']",
    "[aria-current='page']",
    "[data-state='active']",
    "[class*='tab'][class*='active']",
    "[class*='menu-item'][class*='selected']",
    "[class*='menuItem'][class*='selected']",
  ].join(", ");
  const BREADCRUMB_SELECTOR = [
    "[aria-label*='面包屑']",
    "[class*='breadcrumb']",
    "[class*='Breadcrumb']",
    "[data-testid*='breadcrumb']",
  ].join(", ");
  const PAGE_HEADING_SELECTOR = "main h1, main h2, [role='main'] h1, [role='main'] h2, [class*='page-title'], [class*='pageTitle']";

  function scopedLabels(selector, limit = 16) {
    try {
      return Array.from(document.querySelectorAll(selector))
        .filter((element) => !element.getClientRects || element.getClientRects().length > 0)
        .map((element) => String(element.innerText || element.textContent || "").replace(/\s+/g, " ").trim())
        .filter((text) => text && text.length <= 240)
        .slice(0, limit);
    } catch {
      return [];
    }
  }

  function pageTypeFromScopedLabels(labels) {
    const types = new Set();
    for (const label of labels || []) {
      if (/(?:素材管理|素材库|素材分析|创意管理|视频库|内容素材)/.test(label)) types.add("materials");
      if (/(?:直播(?:推广|投放|计划|经营|管理)|直播间计划)/.test(label)) types.add("qianchuan_live");
      if (/(?:商品(?:推广|投放|计划)|商品计划|计划管理|广告计划|标准推广|全域推广)/.test(label)) types.add("campaigns");
    }
    return types.size === 1 ? [...types][0] : "unknown";
  }

  function currentTableSchemaEvidence() {
    try {
      return globalThis.DianAgentExtractor.tableSchemaEvidence?.() || null;
    } catch {
      return null;
    }
  }

  function detectPageType(schemaEvidence = null) {
    const path = location.pathname.toLowerCase();
    const pageText = document.body?.innerText || "";
    if (path.includes("video-library") || /视频库/.test(document.title)) return "video_library";
    if (path.includes("material") || path.includes("creative")) return "materials";
    if (path.includes("board-next") || /直播大屏/.test(document.title)) return "live_dashboard";
    // 千川是单页应用。切换“商品推广/直播推广”时，地址和激活标签
    // 可能已经改变，但旧表格仍停留数秒。优先相信足够强的表格结构，
    // 让全店巡检把未完成切页识别为类型不匹配并重试，而不是把素材
    // 报表伪装成计划列表交给后续自动化门禁。
    const tableEvidence = schemaEvidence || currentTableSchemaEvidence();
    if (tableEvidence?.confidence === "conflict") return "unknown";
    if (tableEvidence?.confidence === "high" && tableEvidence.page_type !== "unknown") return tableEvidence.page_type;
    const activeType = pageTypeFromScopedLabels(scopedLabels(ACTIVE_NAV_SELECTOR));
    if (activeType !== "unknown") return activeType;
    const breadcrumbType = pageTypeFromScopedLabels(scopedLabels(BREADCRUMB_SELECTOR));
    if (breadcrumbType !== "unknown") return breadcrumbType;
    const headingType = pageTypeFromScopedLabels(scopedLabels(PAGE_HEADING_SELECTOR, 8));
    if (headingType !== "unknown") return headingType;
    if (tableEvidence?.confidence === "medium" && tableEvidence.page_type !== "unknown") return tableEvidence.page_type;
    // Whole-page text is a last-resort legacy fallback because it also contains
    // every sidebar entry. It must never override scoped navigation or schema.
    if (/视频(?:追投|类型)/.test(pageText) && /(?:素材建议|素材评估|创作者声明)/.test(pageText)) return "materials";
    if (/抖音号/.test(pageText) && /投放状态/.test(pageText) && /(?:营销服务|调控工具)/.test(pageText)) return "qianchuan_live";
    if (/设置直播规划/.test(pageText)) return "qianchuan_live";
    if (path.includes("live") || path.includes("screen")) return "qianchuan_live";
    if (path === "/home" || path.endsWith("/home")) return "overview";
    if (path.includes("uni-prom") || path.includes("promotion") || path.includes("manage")) return "campaigns";
    if (path.includes("report") || path.includes("data")) return "report";
    if (path.includes("account") || path.includes("fund")) return "account";
    if (location.hostname.includes("buyin")) return "affiliate";
    return "unknown";
  }

  function promotionModesInText(value) {
    const text = String(value || "").normalize("NFKC");
    return [
      ["chengfang", "乘方"],
      ["full_domain", "全域推广"],
      ["standard", "标准推广"],
    ].filter(([, label]) => text.includes(label));
  }

  function detectPromotionContext(schemaEvidence = null) {
    const visibleText = `${document.title || ""}\n${document.body?.innerText || ""}`.slice(0, 120000);
    const tableEvidence = schemaEvidence || currentTableSchemaEvidence();
    const scopedSources = [
      ["active_navigation", scopedLabels(ACTIVE_NAV_SELECTOR).join("\n")],
      ["breadcrumb", scopedLabels(BREADCRUMB_SELECTOR).join("\n")],
      ["page_heading", scopedLabels(PAGE_HEADING_SELECTOR, 8).join("\n")],
      ["table_schema", (tableEvidence?.headers || []).join("\n")],
    ].map(([source, text]) => ({ source, matched: promotionModesInText(text) }))
      .filter((item) => item.matched.length > 0);
    const scopedModes = [...new Set(scopedSources.flatMap((item) => item.matched.map(([mode]) => mode)))];
    let matched = [];
    let evidenceSource = "unverified";
    if (scopedModes.length === 1) {
      matched = scopedSources.flatMap((item) => item.matched).filter(([mode]) => mode === scopedModes[0]).slice(0, 1);
      evidenceSource = scopedSources.length === 1 ? scopedSources[0].source : "consistent_scoped_labels";
    } else if (scopedModes.length > 1) {
      evidenceSource = "conflicting_scoped_labels";
    } else {
      matched = promotionModesInText(visibleText);
      evidenceSource = matched.length === 1 ? "visible_label" : matched.length > 1 ? "conflicting_visible_labels" : "unverified";
    }
    const promotionMode = matched.length === 1 && scopedModes.length <= 1 ? matched[0][0] : "unknown";
    return {
      schema_version: 1,
      promotion_mode: promotionMode,
      strategy_id: "",
      metric: { definition: "unknown", label: "", value: null, period: "" },
      cost_ledger: {},
      platform_managed_fields: promotionMode === "chengfang" ? ["预算分配", "流量分配", "素材协同"] : [],
      evidence: {
        source: evidenceSource,
        label: matched.length === 1 ? matched[0][1] : "",
        captured_at_ms: Date.now(),
      },
    };
  }

  function assertLegacyExecutionMode(request) {
    const mode = String(request?.promotion_context?.promotion_mode || "unknown");
    if (mode === "chengfang") throw new Error("UNSUPPORTED_FOR_CHENGFANG：乘方模式禁止使用旧单计划预算、暂停和恢复执行器。");
    if (!["standard", "full_domain"].includes(mode)) throw new Error("PROMOTION_MODE_UNVERIFIED：尚未确认当前投放模式，已停止执行。");
    const scope = request?.promotion_context?.account_scope || {};
    const metricVersion = String(request?.promotion_context?.metric_contract?.version || "");
    const strategyId = String(request?.promotion_context?.strategy_id || "");
    const requestAccount = String(request?.account_key || "").trim().toLowerCase();
    const contextAccount = String(scope.account_id || "").trim().toLowerCase();
    if (requestAccount && contextAccount && requestAccount !== contextAccount) {
      throw new Error("ACTION_CONTEXT_ACCOUNT_MISMATCH: action target account does not match promotion context account.");
    }
    if (!scope.store_id || !scope.account_id || scope.conflict || !strategyId || !metricVersion) {
      throw new Error("PROMOTION_SCOPE_UNVERIFIED：店铺、账户、策略或指标合同绑定不完整，已停止执行。");
    }
  }

  function currentCollectionContextFingerprint() {
    const semanticLabels = (selector, limit = 16) => scopedLabels(selector, limit)
      .filter((label) => /(?:标准推广|全域推广|乘方|商品推广|直播推广|商品计划|直播计划|计划管理|广告计划|素材管理|素材库|创意管理|内容素材)/.test(label))
      .sort();
    return JSON.stringify({
      href: String(location.href || ""),
      identity: canonicalIdentityWitness(detectIdentityClaims()),
      // Generic active selectors also match numeric pagination. Keep only
      // business navigation tokens so page 1→2 is collection progress, while
      // standard/full-domain/Chengfang or product/live/material switches are
      // scope changes.
      active_navigation: semanticLabels(ACTIVE_NAV_SELECTOR),
      breadcrumb: semanticLabels(BREADCRUMB_SELECTOR),
      heading: semanticLabels(PAGE_HEADING_SELECTOR, 8),
    });
  }

  function sampleCollectionContext() {
    const current = currentCollectionContextFingerprint();
    if (lastCollectionContextFingerprint && current !== lastCollectionContextFingerprint) collectionContextEpoch += 1;
    lastCollectionContextFingerprint = current;
    return collectionContextEpoch;
  }

  function ensureContextObserver() {
    if (contextObserverStarted) return;
    contextObserverStarted = true;
    sampleCollectionContext();
    if (typeof MutationObserver === "function" && document.documentElement) {
      new MutationObserver(() => sampleCollectionContext()).observe(document.documentElement, {
        subtree: true, childList: true, characterData: true, attributes: true,
      });
    }
    if (typeof globalThis.addEventListener === "function") {
      ["popstate", "hashchange", "storage"].forEach((name) => globalThis.addEventListener(name, sampleCollectionContext));
    }
  }

  async function capture(reason = "auto", options = {}) {
    const executionRequest = options.executionRequest || null;
    ensureContextObserver();
    const entryEpoch = sampleCollectionContext();
    const entryWitness = captureExecutionWitness();
    const assertCaptureStable = () => {
      sampleCollectionContext();
      if (collectionContextEpoch !== entryEpoch) {
        const error = new Error(executionRequest
          ? "EXECUTION_PAGE_WITNESS_CHANGED: 执行身份检查期间页面范围发生过变化，页面未提交。"
          : "QIANCHUAN_CAPTURE_CONTEXT_CHANGED: 采集期间店铺、账户或页面范围发生过变化，本次快照已丢弃。");
        error.code = "COLLECTION_CONTEXT_CHANGED";
        throw error;
      }
      const currentWitness = captureExecutionWitness();
      if (executionWitnessFingerprint(currentWitness) !== executionWitnessFingerprint(entryWitness)) {
        const error = new Error(executionRequest
          ? "EXECUTION_PAGE_WITNESS_CHANGED: 执行身份检查期间店铺、账户、路由、页面类型或投放模式已变化，页面未提交。"
          : "QIANCHUAN_CAPTURE_CONTEXT_CHANGED: 采集期间店铺、账户、路由、页面类型或投放模式已变化，本次快照已丢弃。");
        error.code = "COLLECTION_CONTEXT_CHANGED";
        throw error;
      }
      return currentWitness;
    };
    if (executionRequest) assertAuthorizedDomContext(executionRequest, entryWitness);
    const stored = await chrome.storage.local.get("settings");
    assertCaptureStable();
    if (executionRequest) assertExecutionWitnessStable(entryWitness, executionRequest);
    const privacyMode = stored.settings?.privacyMode !== false;
    const tableEvidence = currentTableSchemaEvidence();
    const data = await globalThis.DianAgentExtractor.collect(
      SOURCE,
      detectPageType(tableEvidence),
      privacyMode,
      reason,
      {
        assertContextStable: assertCaptureStable,
        targetPlanId: String(options.targetPlanId || ""),
      },
    );
    assertCaptureStable();
    if (executionRequest) assertExecutionWitnessStable(entryWitness, executionRequest);
    data.promotion_context = detectPromotionContext(tableEvidence);
    const identity = detectIdentityClaims();
    data.identity_claims = identity.claims;
    data.identity_status = identity.status;
    data.identity_conflicts = identity.conflicts || [];
    data.document_instance_id = DOCUMENT_INSTANCE_ID;
    data.document_url = String(location.href || "");
    data.navigation_started_at_ms = NAVIGATION_STARTED_AT_MS;
    if (options.deferPush === true) {
      const executionWitness = assertCaptureStable();
      return {
        ok: true,
        page_type: data.page_type,
        quality: data.quality,
        snapshot: data,
        execution_witness: executionWitness,
      };
    }
    assertCaptureStable();
    if (executionRequest) assertExecutionWitnessStable(entryWitness, executionRequest);
    const response = await chrome.runtime.sendMessage({
      type: "page-data",
      source: SOURCE,
      data,
      expected_scope: {
        store_key: String(options.expectedStoreKey || ""),
        account_key: String(options.expectedAccountKey || ""),
      },
    });
    const bridgeWitness = assertCaptureStable();
    if (executionRequest) assertExecutionWitnessStable(entryWitness, executionRequest);
    return {
      ok: true,
      page_type: data.page_type,
      quality: data.quality,
      account: response?.account || null,
      store: response?.store || null,
      bridge: response,
      execution_witness: bridgeWitness,
    };
  }

  async function ensureResolvedExecutionScope(request) {
    const expectedStoreKey = String(request?.promotion_context?.account_scope?.store_id || "").trim().toLowerCase();
    const expectedAccountKey = String(request?.account_key || "").trim().toLowerCase();
    const actionId = String(request?.action_id || "").trim().toLowerCase();
    const binding = assertExecutionGrantBinding(request);
    const authorizationId = binding.authorization_id;
    if (!expectedStoreKey || !expectedAccountKey) throw new Error("授权缺少店铺或千川账户作用域，页面未提交。");
    if (!/^[a-f0-9]{24}$/.test(actionId) || !/^[a-f0-9]{32}$/.test(authorizationId)) {
      throw new Error("授权缺少动作或一次性授权绑定，页面未提交。");
    }
    const candidate = await capture("execution-identity-check", {
      expectedStoreKey,
      expectedAccountKey,
      executionRequest: request,
      deferPush: true,
    });
    assertExecutionWitnessStable(candidate.execution_witness, request);
    const resolved = await chrome.runtime.sendMessage({
      type: "resolve-execution-identity",
      source: SOURCE,
      action_id: actionId,
      authorization_id: authorizationId,
      execution_request: request,
      data: candidate.snapshot,
    });
    if (!resolved?.ok || resolved?.execution_request_bound !== true || !/^[a-f0-9]{64}$/.test(String(resolved?.execution_request_digest || ""))) {
      throw new Error(resolved?.error || "当前执行请求未通过 Agent 完整绑定校验。");
    }
    assertExecutionWitnessStable(candidate.execution_witness, request);
    const account = resolved.account;
    const store = resolved.store;
    if (!account || String(account.key || "").trim().toLowerCase() !== expectedAccountKey) {
      throw new Error("当前千川账号与授权账号不一致。");
    }
    if (!store || String(store.key || "").trim().toLowerCase() !== expectedStoreKey) {
      throw new Error("当前店铺与授权店铺不一致。");
    }
    return { account, store, execution_witness: candidate.execution_witness };
  }

  function normalizedNumber(value) {
    const parsed = Number(String(value ?? "").replace(/[^\d.-]/g, ""));
    return Number.isFinite(parsed) ? parsed : null;
  }

  function visible(element) {
    return Boolean(element && element.getClientRects().length > 0);
  }

  function normalizedPlanIdTokens(value) {
    return String(value || "")
      .normalize("NFKC")
      .toLowerCase()
      .match(/[a-z0-9_-]+/g) || [];
  }

  function planIdCellEvidence(row) {
    if (!row || typeof row !== "object") return [];
    const cells = Array.from(row.querySelectorAll?.("td, [role='cell'], [role='gridcell']") || []);
    const table = row.closest?.("table, [role='grid'], [role='table']") || null;
    const headers = Array.from(table?.querySelectorAll?.("thead th, [role='columnheader']") || []);
    const evidence = [];
    cells.forEach((cell, index) => {
      const header = String(
        headers[index]?.innerText
        || headers[index]?.textContent
        || cell.getAttribute?.("data-field")
        || cell.getAttribute?.("data-column")
        || cell.getAttribute?.("aria-label")
        || "",
      ).trim();
      if (globalThis.DianAgentExtractor.entityKindForHeader(header) !== "qianchuan_plan_id") return;
      const value = String(cell.innerText || cell.textContent || "").trim();
      if (value) evidence.push({ header, value });
    });
    return evidence;
  }

  function rowHasExactPlanId(rowOrText, planId) {
    const target = String(planId || "").normalize("NFKC").trim().toLowerCase();
    if (!target || !/^[a-z0-9_-]+$/.test(target)) return false;
    for (const item of planIdCellEvidence(rowOrText)) {
      const pseudonymized = globalThis.DianAgentExtractor.pseudonymizePlanIdentifier(item.value, item.header);
      if (normalizedPlanIdTokens(item.value).includes(target)
        || normalizedPlanIdTokens(pseudonymized).includes(target)) return true;
    }
    const rowText = typeof rowOrText === "string"
      ? rowOrText
      : String(rowOrText?.innerText || rowOrText?.textContent || "");
    const pseudonymized = globalThis.DianAgentExtractor.pseudonymizePlanIdentifier(rowText, "计划");
    return normalizedPlanIdTokens(rowText).includes(target)
      || normalizedPlanIdTokens(pseudonymized).includes(target);
  }

  function assertExecutionGrantBinding(request) {
    const actionId = String(request?.action_id || "").trim().toLowerCase();
    const authorizationId = String(request?.authorization_id || "").trim().toLowerCase();
    const attemptId = String(request?.execution_attempt_id || "").trim().toLowerCase();
    if (
      !/^[a-f0-9]{24}$/.test(actionId)
      || !/^[a-f0-9]{32}$/.test(authorizationId)
      || authorizationId !== attemptId
    ) {
      throw new Error("一次性执行授权与动作绑定不一致；页面未提交。");
    }
    return { action_id: actionId, authorization_id: authorizationId };
  }

  function assertExecutionGrantFresh(request) {
    assertExecutionGrantBinding(request);
    const deadline = Number(request?.execute_before_ms || 0);
    if (!Number.isFinite(deadline) || deadline <= Date.now()) {
      throw new Error("本次单次执行授权已过期；页面未提交，请重新完成执行前检查。");
    }
  }

  async function probeBudgetExecution(request) {
    assertLegacyExecutionMode(request);
    if (!["adjust_budget", "restore_budget"].includes(request?.operation_type) || request?.mode !== "supervised_submit") {
      throw new Error("当前页面执行器只支持受监督降低预算或恢复原预算。");
    }
    const resolvedScope = await ensureResolvedExecutionScope(request);
    const executionWitness = resolvedScope.execution_witness;
    assertExecutionWitnessStable(executionWitness, request);
    const planId = String(request.plan_id || "").trim();
    const planName = String(request.plan_name || "").trim();
    const expected = Number(request.expected_current_value);
    const target = Number(request.target_value);
    if (!planId || !planName || !Number.isFinite(expected)) throw new Error("授权缺少计划身份或当前预算。");
    const inRange = request.operation_type === "restore_budget"
      ? target > expected && (target - expected) / expected <= 0.50
      : target > 0 && target < expected && (expected - target) / expected <= 0.30;
    if (!Number.isFinite(target) || !inRange) {
      throw new Error("目标预算不符合首批止损或回滚范围。");
    }
    const rows = Array.from(document.querySelectorAll("tr, [role='row'], [class*='table-row'], [class*='TableRow']"))
      .filter(visible);
    const matches = rows.filter((row) => {
      const text = String(row.innerText || "").replace(/\s+/g, " ");
      return rowHasExactPlanId(row, planId) && text.includes(planName);
    });
    if (matches.length !== 1) throw new Error(matches.length ? "页面存在多个同名计划，已停止执行。" : "当前页面未找到授权计划，请打开对应计划列表。");
    const row = matches[0];
    const budgetInputs = Array.from(row.querySelectorAll("input")).filter((input) => {
      if (!visible(input) || input.disabled) return false;
      const label = `${input.getAttribute("aria-label") || ""} ${input.getAttribute("placeholder") || ""}`;
      return /预算/.test(label) && Math.abs((normalizedNumber(input.value) ?? NaN) - expected) <= 0.01;
    });
    if (budgetInputs.length !== 1) throw new Error(budgetInputs.length ? "发现多个预算输入框，已停止执行。" : "未找到与授权当前预算一致的输入框。");
    const scope = budgetInputs[0].closest("[role='dialog'], [class*='modal'], [class*='drawer']") || row;
    const submitButtons = Array.from(scope.querySelectorAll("button")).filter((button) => (
      visible(button)
      && /^(确认|确定|保存|提交)$/.test(String(button.innerText || button.textContent || "").trim())
      && !button.disabled
      && button.getAttribute("aria-disabled") !== "true"
    ));
    if (submitButtons.length !== 1) throw new Error(submitButtons.length ? "发现多个提交按钮，已停止执行。" : "未找到唯一提交按钮。");
    assertExecutionWitnessStable(executionWitness, request);
    return {
      ok: true,
      ready: true,
      plan_id: planId,
      current_value: expected,
      target_value: target,
      execution_witness: executionWitness,
    };
  }

  function findPlanRow(request) {
    const planId = String(request.plan_id || "").trim();
    const planName = String(request.plan_name || "").trim();
    const rows = Array.from(document.querySelectorAll("tr, [role='row'], [class*='table-row'], [class*='TableRow']")).filter(visible);
    const matches = rows.filter((row) => {
      const text = String(row.innerText || "").replace(/\s+/g, " ");
      return rowHasExactPlanId(row, planId) && text.includes(planName);
    });
    if (matches.length !== 1) throw new Error(matches.length ? "页面存在多个同名计划，已停止执行。" : "当前页面未找到授权计划，请打开对应计划列表。");
    return matches[0];
  }

  function matchingBudgetInputs(row, expected) {
    return Array.from(row.querySelectorAll("input")).filter((input) => {
      if (!visible(input) || input.disabled || input.getAttribute("aria-disabled") === "true") return false;
      const label = (input.getAttribute("aria-label") || "") + " " + (input.getAttribute("placeholder") || "");
      return /预算/.test(label) && Math.abs((normalizedNumber(input.value) ?? NaN) - expected) <= 0.01;
    });
  }

  function writeBudgetInputValue(input, value, setter) {
    setter.call(input, String(value));
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function budgetRecoveryUnverified(reason = "") {
    const detail = String(reason || "").trim();
    const error = new Error(
      `预算表单已停止提交，但无法证明页面已恢复原值。${detail ? `${detail}；` : ""}请关闭当前编辑框或刷新计划页后再操作。`,
    );
    error.code = "BUDGET_RECOVERY_UNVERIFIED";
    error.recovery_unverified = true;
    error.submitted = null;
    error.submission_state = "unknown";
    return error;
  }

  function restoreLiveBudgetInput(request, expected, target, setter, authorizedWitness) {
    try {
      // Cleanup is permitted only inside the exact document/account/route/mode
      // that received the authorized mutation. Never establish a new cleanup
      // baseline after an identity change: that could write the old value into
      // another account that happens to contain the same plan ID.
      assertExecutionWitnessStable(authorizedWitness, request);
      const liveRow = findPlanRow(request);
      const targetInputs = matchingBudgetInputs(liveRow, target);
      const expectedInputs = matchingBudgetInputs(liveRow, expected);
      if (targetInputs.length === 1) {
        assertExecutionWitnessStable(authorizedWitness, request);
        writeBudgetInputValue(targetInputs[0], expected, setter);
      } else if (!(targetInputs.length === 0 && expectedInputs.length === 1)) {
        throw budgetRecoveryUnverified(
          targetInputs.length > 1 ? "当前计划出现多个目标预算输入框" : "当前计划的预算输入框已重新渲染",
        );
      }

      // Framework input handlers may synchronously replace the row again.
      // Re-resolve from the document and prove one exact original value remains.
      const verifiedRow = findPlanRow(request);
      const restoredInputs = matchingBudgetInputs(verifiedRow, expected);
      const remainingTargetInputs = matchingBudgetInputs(verifiedRow, target);
      assertExecutionWitnessStable(authorizedWitness, request);
      if (restoredInputs.length !== 1 || remainingTargetInputs.length !== 0) {
        throw budgetRecoveryUnverified("恢复后无法重新确认唯一原预算");
      }
      return true;
    } catch (error) {
      if (error?.recovery_unverified === true) throw error;
      throw budgetRecoveryUnverified(error?.message || "页面结构已变化");
    }
  }

  function pauseActionButtons(row) {
    return Array.from(row.querySelectorAll("button, [role='button'], [role='switch']")).filter((button) => (
      visible(button)
      && !button.disabled
      && button.getAttribute("aria-disabled") !== "true"
      && /^(暂停|停用)$/.test(String(button.innerText || button.textContent || "").trim())
    ));
  }

  function visiblePauseConfirmationContainers() {
    const candidates = Array.from(document.querySelectorAll(
      "[role='alertdialog'], [role='dialog'], [aria-modal='true'], [class*='modal' i]",
    )).filter(visible);
    const unique = [];
    for (const candidate of candidates) {
      if (unique.includes(candidate)) continue;
      const containsExisting = unique.some((item) => typeof candidate.contains === "function" && candidate.contains(item));
      if (containsExisting) continue;
      for (let index = unique.length - 1; index >= 0; index -= 1) {
        if (typeof unique[index].contains === "function" && unique[index].contains(candidate)) unique.splice(index, 1);
      }
      unique.push(candidate);
    }
    return unique;
  }

  function pauseConfirmationText(element) {
    return String(element?.innerText || element?.textContent || "").replace(/\s+/g, " ").trim();
  }

  function isPauseConfirmationContainer(element, request) {
    const content = pauseConfirmationText(element);
    const planName = String(request?.plan_name || "").trim();
    return /(暂停|停用)/.test(content)
      && /(确认|确定|是否)/.test(content)
      && (/(计划|投放)/.test(content) || Boolean(planName && content.includes(planName)));
  }

  function pauseConfirmationButtons(container) {
    return Array.from(container.querySelectorAll("button, [role='button']")).filter((button) => {
      if (!visible(button) || button.disabled || button.getAttribute("aria-disabled") === "true") return false;
      const label = String(button.innerText || button.textContent || button.getAttribute("aria-label") || "")
        .replace(/\s+/g, " ")
        .trim();
      return /^(?:确认|确定|暂停(?:计划|投放)?|停用(?:计划|投放)?|(?:确认|确定|继续)(?:暂停|停用)(?:计划|投放)?)$/.test(label);
    });
  }

  async function waitForNewPauseConfirmation(previouslyVisible) {
    for (let attempt = 0; attempt < 5; attempt += 1) {
      const added = visiblePauseConfirmationContainers().filter((container) => !previouslyVisible.has(container));
      if (added.length) return added;
      if (attempt < 4) await new Promise((resolve) => setTimeout(resolve, 75));
    }
    return [];
  }

  function assertPauseableRowState(row, request) {
    const expected = String(request?.expected_current_value || "").trim();
    const text = statusText(row);
    const activeStatuses = ["投放中", "启用", "生效中", "运行中"];
    if (!activeStatuses.includes(expected) || !text.includes(expected)) {
      throw new Error("授权计划的当前状态已变化，页面未做任何修改。");
    }
    return text;
  }

  function statusText(row) {
    return String(row.innerText || "").replace(/\s+/g, " ").trim();
  }

  function unverifiedDomSubmission(details = {}) {
    // A DOM click is never authoritative platform evidence. The row may still
    // contain the pre-click action label, an unrelated success toast may be
    // present, or the platform may have opened another confirmation dialog.
    // Only the subsequent fresh plan readback may mark this action successful.
    return {
      ok: true,
      mode: "supervised_submit",
      submitted: true,
      submitted_unverified: true,
      platform_success_observed: false,
      verification_required: true,
      submission_evidence: "dom_click_only",
      ...details,
    };
  }

  function canonicalIdentityWitness(identity = detectIdentityClaims()) {
    const claims = (identity?.claims || []).map((claim) => ({
      kind: String(claim?.kind || ""),
      raw_id: String(claim?.raw_id || ""),
      evidence_source: String(claim?.evidence_source || ""),
      confidence: String(claim?.confidence || ""),
    })).sort((left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right)));
    return {
      claims,
      conflicts: [...new Set((identity?.conflicts || []).map((value) => String(value || "")))]
        .filter(Boolean)
        .sort(),
      status: String(identity?.status || "unresolved"),
    };
  }

  function captureExecutionWitness(schemaEvidence = undefined) {
    const tableEvidence = schemaEvidence === undefined ? currentTableSchemaEvidence() : schemaEvidence;
    return {
      schema_version: 1,
      document_instance_id: DOCUMENT_INSTANCE_ID,
      document_url: String(location.href || ""),
      navigation_started_at_ms: NAVIGATION_STARTED_AT_MS,
      route: {
        hostname: String(location.hostname || ""),
        pathname: String(location.pathname || ""),
        search: String(location.search || ""),
        hash: String(location.hash || ""),
      },
      identity: canonicalIdentityWitness(),
      promotion_mode: String(detectPromotionContext(tableEvidence).promotion_mode || "unknown"),
      page_type: String(detectPageType(tableEvidence) || "unknown"),
    };
  }

  function executionWitnessFingerprint(witness) {
    return JSON.stringify(witness || null);
  }

  function assertAuthorizedDomContext(request, witness) {
    const identity = witness?.identity && typeof witness.identity === "object" ? witness.identity : {};
    const accountClaims = Array.isArray(identity.claims)
      ? identity.claims.filter((claim) => ["qianchuan_advertiser_id", "qianchuan_account_id"].includes(String(claim?.kind || "")))
      : [];
    if (
      identity.status !== "resolved_by_bridge"
      || identity.conflicts?.length
      || accountClaims.length !== 1
      || String(accountClaims[0]?.confidence || "") !== "high"
      || !["url_parameter", "data_attribute"].includes(String(accountClaims[0]?.evidence_source || ""))
    ) {
      throw new Error("EXECUTION_ACCOUNT_IDENTITY_NOT_HIGH_CONFIDENCE：执行必须由当前页面 URL 或身份属性明确证明唯一千川账号；缓存账号不能用于投放。 ");
    }
    const authorizedMode = String(request?.promotion_context?.promotion_mode || "unknown");
    const actualMode = String(witness?.promotion_mode || "unknown");
    if (actualMode === "chengfang") {
      throw new Error("UNSUPPORTED_FOR_CHENGFANG：当前页面已切换到乘方，旧单计划执行器已停止。");
    }
    if (!["standard", "full_domain"].includes(actualMode) || actualMode !== authorizedMode) {
      throw new Error(`PROMOTION_MODE_WITNESS_MISMATCH：授权模式 ${authorizedMode} 与当前页面模式 ${actualMode} 不一致。`);
    }
    if (String(witness?.page_type || "unknown") === "unknown") {
      throw new Error("PAGE_TYPE_WITNESS_UNVERIFIED：当前投放页面类型无法确认，已停止执行。");
    }
    const canonicalPageType = (value) => String(value || "unknown") === "qianchuan_campaigns"
      ? "campaigns"
      : String(value || "unknown");
    const authorizedPageType = canonicalPageType(request?.page_type);
    const actualPageType = canonicalPageType(witness?.page_type);
    if (authorizedPageType === "unknown" || actualPageType !== authorizedPageType) {
      throw new Error(`PAGE_TYPE_WITNESS_MISMATCH：授权页面 ${authorizedPageType} 与当前页面 ${actualPageType} 不一致。`);
    }
  }

  function assertExecutionWitnessStable(expected, request, options = {}) {
    if (!expected || typeof expected !== "object") {
      throw new Error("EXECUTION_WITNESS_MISSING：缺少执行页面身份见证，已停止执行。");
    }
    const current = captureExecutionWitness();
    if (options.enforceAuthorizedMode !== false) assertAuthorizedDomContext(request, current);
    if (executionWitnessFingerprint(current) !== executionWitnessFingerprint(expected)) {
      throw new Error("EXECUTION_PAGE_WITNESS_CHANGED：店铺、账户、地址、页面类型或投放模式已变化，已停止执行。");
    }
    return current;
  }

  function markSubmissionUnknown(error, code = "SUBMISSION_STATE_UNKNOWN") {
    const marked = error instanceof Error ? error : new Error(String(error || "执行状态未知。"));
    if (!marked.code) marked.code = code;
    marked.submitted = null;
    marked.submission_state = "unknown";
    return marked;
  }

  async function pausePlanProbe(request) {
    assertLegacyExecutionMode(request);
    if (request?.operation_type !== "pause_plan" || request?.mode !== "supervised_submit") throw new Error("当前页面不是受监督暂停动作。");
    const resolvedScope = await ensureResolvedExecutionScope(request);
    const executionWitness = resolvedScope.execution_witness;
    assertExecutionWitnessStable(executionWitness, request);
    if (String(request.expected_current_value || "") !== "投放中" && !["启用", "生效中", "运行中"].includes(String(request.expected_current_value || ""))) throw new Error("当前计划状态不是可暂停状态。");
    if (String(request.target_value || "") !== "暂停") throw new Error("暂停目标无效。");
    const row = findPlanRow(request);
    assertPauseableRowState(row, request);
    const buttons = pauseActionButtons(row);
    if (buttons.length !== 1) throw new Error(buttons.length ? "发现多个暂停按钮，已停止执行。" : "未找到唯一暂停按钮。");
    assertExecutionWitnessStable(executionWitness, request);
    return {
      ok: true,
      ready: true,
      plan_id: request.plan_id,
      status: statusText(row),
      execution_witness: executionWitness,
    };
  }

  async function supervisedPauseSubmit(request) {
    assertExecutionGrantFresh(request);
    const probe = await pausePlanProbe(request);
    const executionWitness = probe.execution_witness;
    assertExecutionWitnessStable(executionWitness, request);
    // Re-resolve every mutable DOM target after the async account/probe step.
    // A virtualized table may have re-rendered or duplicated controls while the
    // single-use grant was waiting, so the probe result is not a click handle.
    const row = findPlanRow(request);
    assertPauseableRowState(row, request);
    const buttons = pauseActionButtons(row);
    if (buttons.length !== 1) throw new Error(buttons.length ? "执行前发现多个暂停按钮，页面未做任何修改。" : "未找到唯一暂停按钮，页面未做任何修改。");
    assertExecutionGrantFresh(request);
    // This is deliberately synchronous: no task/microtask may run between the
    // final raw identity/route/mode check and the platform click.
    assertExecutionWitnessStable(executionWitness, request);
    const previousConfirmations = new Set(visiblePauseConfirmationContainers());
    let clickMayHaveOccurred = false;
    try {
      clickMayHaveOccurred = true;
      buttons[0].click();
      const newConfirmations = await waitForNewPauseConfirmation(previousConfirmations);
      if (!newConfirmations.length) {
        return unverifiedDomSubmission({
          plan_id: request.plan_id,
          target_value: "暂停",
          submission_evidence: "dom_pause_click_no_confirmation_observed",
        });
      }
      const matchingConfirmations = newConfirmations.filter((container) => isPauseConfirmationContainer(container, request));
      if (newConfirmations.length !== 1 || matchingConfirmations.length !== 1) {
        throw new Error("暂停操作打开了无法唯一确认的弹窗，已停止后续点击，请先检查当前计划状态。");
      }
      const confirmation = matchingConfirmations[0];
      const confirmButtons = pauseConfirmationButtons(confirmation);
      if (confirmButtons.length !== 1) {
        throw new Error(confirmButtons.length ? "暂停确认弹窗存在多个确认按钮，已停止后续点击。" : "暂停确认弹窗没有唯一确认按钮，已停止后续点击。");
      }
      const liveRow = findPlanRow(request);
      assertPauseableRowState(liveRow, request);
      const confirmButton = confirmButtons[0];
      if (!visible(confirmButton) || confirmButton.disabled || confirmButton.getAttribute("aria-disabled") === "true") {
        throw new Error("暂停确认按钮在点击前已失效，已停止后续点击。");
      }
      assertExecutionGrantFresh(request);
      assertExecutionWitnessStable(executionWitness, request);
      confirmButton.click();
    } catch (error) {
      if (clickMayHaveOccurred) throw markSubmissionUnknown(error, "PAUSE_SUBMISSION_STATE_UNKNOWN");
      throw error;
    }
    return unverifiedDomSubmission({
      plan_id: request.plan_id,
      target_value: "暂停",
      submission_evidence: "dom_pause_click_and_confirmation",
    });
  }

  async function supervisedBudgetSubmit(request) {
    assertExecutionGrantFresh(request);
    const probe = await probeBudgetExecution(request);
    const executionWitness = probe.execution_witness;
    assertExecutionWitnessStable(executionWitness, request);
    if (!["adjust_budget", "restore_budget"].includes(request?.operation_type) || request?.mode !== "supervised_submit") {
      throw new Error("当前页面执行器只支持受监督降低预算或恢复原预算。");
    }
    const planId = String(request.plan_id || "").trim();
    const planName = String(request.plan_name || "").trim();
    if (!planId || !planName) throw new Error("授权缺少计划唯一 ID 或名称。");
    // The probe crossed an async identity check. Rebind the exact plan row and
    // require one—and only one—input still holding the authorized value before
    // performing the first DOM mutation.
    const row = findPlanRow(request);
    const expected = Number(request.expected_current_value);
    const budgetInputs = matchingBudgetInputs(row, expected);
    if (budgetInputs.length !== 1) {
      throw new Error(budgetInputs.length ? "执行前发现多个预算输入框，页面未做任何修改。" : "未找到与授权当前预算一致的输入框，页面未做任何修改。");
    }
    const budgetInput = budgetInputs[0];
    const target = Number(request.target_value);
    const inRange = request.operation_type === "restore_budget"
      ? target > expected && (target - expected) / expected <= 0.50
      : target > 0 && target < expected && (expected - target) / expected <= 0.30;
    if (!Number.isFinite(target) || !inRange) {
      throw new Error("目标预算不符合首批止损或回滚范围。");
    }
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    if (!setter) throw new Error("浏览器不支持安全填写预算。");
    assertExecutionGrantFresh(request);
    // No await is permitted from this exact witness check through the first
    // authorized DOM write. The input/change handlers can still synchronously
    // change route/account/mode, so the witness is checked again before click.
    assertExecutionWitnessStable(executionWitness, request);
    let mutationStarted = false;
    let clickMayHaveOccurred = false;
    try {
      mutationStarted = true;
      writeBudgetInputValue(budgetInput, target, setter);
      // Input/change handlers run synchronously and may switch the SPA account
      // or promotion surface. Re-check before any highlighting or control lookup.
      assertExecutionWitnessStable(executionWitness, request);
      budgetInput.focus();
      budgetInput.style.outline = "3px solid #f59e0b";
      budgetInput.scrollIntoView({ behavior: "smooth", block: "center" });

      // The input/change events may synchronously re-render a virtualized row.
      // Rebind once more to the exact plan and the just-written value before the
      // final submit click; never click a control scoped from a detached row.
      const submitRow = findPlanRow(request);
      const submitBudgetInputs = matchingBudgetInputs(submitRow, target);
      if (submitBudgetInputs.length !== 1) {
        throw new Error(submitBudgetInputs.length ? "提交前发现多个目标预算输入框，已停止执行。" : "提交前无法重新确认目标预算，已停止执行。");
      }
      const submitBudgetInput = submitBudgetInputs[0];
      const submitScope = submitBudgetInput.closest("[role='dialog'], [class*='modal'], [class*='drawer']") || submitRow;
      const buttons = Array.from(submitScope.querySelectorAll("button")).filter(visible);
      const submitButtons = buttons.filter((button) => /^(确认|确定|保存|提交)$/.test(String(button.innerText || button.textContent || "").trim())
        && !button.disabled && button.getAttribute("aria-disabled") !== "true");
      if (submitButtons.length !== 1) {
        throw new Error(submitButtons.length ? "发现多个提交按钮，已停止执行。" : "未找到唯一提交按钮，已停止执行。");
      }
      assertExecutionGrantFresh(request);
      // Final synchronous identity witness: switching account/store, route,
      // page type or standard/full-domain mode to Chengfang is always 0-click.
      assertExecutionWitnessStable(executionWitness, request);
      clickMayHaveOccurred = true;
      submitButtons[0].click();
      return unverifiedDomSubmission({
        plan_id: planId,
        target_value: target,
      });
    } catch (error) {
      if (mutationStarted && !clickMayHaveOccurred) {
        try {
          restoreLiveBudgetInput(request, expected, target, setter, executionWitness);
        } catch (recoveryError) {
          throw markSubmissionUnknown(recoveryError, "BUDGET_RECOVERY_UNVERIFIED");
        }
      }
      if (mutationStarted || clickMayHaveOccurred) throw markSubmissionUnknown(error);
      throw error;
    }
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message.type === "dian-agent-content-status") {
      sendResponse({ ok: true, source: SOURCE, runtime_version: RUNTIME_VERSION, page_type: detectPageType(currentTableSchemaEvidence()) });
      return false;
    }
    if (message.type === "collect-now") {
      capture(message.reason || "manual", {
        deferPush: message.defer_push === true,
        targetPlanId: String(message.target_plan_id || ""),
      })
        .then(sendResponse)
        .catch((error) => sendResponse({
          ok: false,
          error: error.message || String(error),
          code: error?.code || "",
          error_code: error?.code || "",
        }));
      return true;
    }
    if (message.type === "qianchuan-supervised-submit") {
      let executionAttemptId = "";
      try {
        executionAttemptId = assertExecutionGrantBinding(message.request).authorization_id;
      } catch (error) {
        sendResponse({
          ok: false,
          code: "EXECUTION_GRANT_BINDING_INVALID",
          error: error.message || String(error),
          submitted: false,
          submission_state: "not_submitted",
          recovery_unverified: false,
          definite_not_submitted: true,
          submission_phase: "pre_mutation",
          mutation_started: false,
          click_invoked: false,
        });
        return false;
      }
      if (/^[a-f0-9]{32}$/.test(executionAttemptId) && consumedExecutionAttemptIds.has(executionAttemptId)) {
        sendResponse({
          ok: false,
          code: "DUPLICATE_EXECUTION_ATTEMPT",
          error: "该一次性执行授权已在当前页面处理；禁止重复提交，必须等待独立回读。",
          submitted: null,
          submission_state: "unknown",
          recovery_unverified: false,
          definite_not_submitted: false,
          submission_phase: "unknown",
          mutation_started: null,
          click_invoked: null,
        });
        return false;
      }
      if (/^[a-f0-9]{32}$/.test(executionAttemptId)) {
        consumedExecutionAttemptIds.add(executionAttemptId);
        if (consumedExecutionAttemptIds.size > 128) consumedExecutionAttemptIds.delete(consumedExecutionAttemptIds.values().next().value);
      }
      (message.request?.operation_type === "pause_plan" ? supervisedPauseSubmit(message.request) : supervisedBudgetSubmit(message.request))
        .then(sendResponse)
        .catch((error) => {
          const submissionUnknown = error?.submitted === null
            || error?.submission_state === "unknown"
            || error?.recovery_unverified === true;
          sendResponse({
            ok: false,
            code: error?.code || "",
            error: error.message || String(error),
            submitted: submissionUnknown ? null : false,
            submission_state: submissionUnknown ? "unknown" : "not_submitted",
            recovery_unverified: error?.recovery_unverified === true,
            definite_not_submitted: submissionUnknown ? false : true,
            submission_phase: submissionUnknown ? "unknown" : "pre_mutation",
            mutation_started: submissionUnknown ? null : false,
            click_invoked: submissionUnknown ? null : false,
          });
        });
      return true;
    }
    if (message.type === "qianchuan-execution-probe") {
      (message.request?.operation_type === "pause_plan" ? pausePlanProbe(message.request) : probeBudgetExecution(message.request))
        .then(sendResponse)
        .catch((error) => sendResponse({ ok: false, ready: false, error: error.message || String(error) }));
      return true;
    }
    if (message.type === "qianchuan-document-witness") {
      try {
        sendResponse({ ok: true, witness: captureExecutionWitness() });
      } catch (error) {
        sendResponse({ ok: false, error: error.message || String(error) });
      }
      return false;
    }
    return false;
  });

  // The background coordinator owns all collection scheduling. Passive page
  // observers caused duplicate writes and could persist a wrong route before
  // the scan validated page/account/store scope.
})();
