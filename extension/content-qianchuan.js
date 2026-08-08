/** 巨量千川页面采集器 */
(function () {
  "use strict";
  if (globalThis.__DianAgentQianchuanLoaded) return;
  globalThis.__DianAgentQianchuanLoaded = true;
  const SOURCE = "qianchuan";
  const RENDER_DELAY = 3200;
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
    const advertiser = identityClaim("qianchuan_advertiser_id", ["advertiser_id", "advertiserId", "aadvid", "advid", "adv_id"], ["data-advertiser-id", "data-aadvid"], ["advertiser_id", "advertiserId", "aadvid", "advid", "adv_id", "selected_advertiser_id"]);
    const account = advertiser?.raw_id ? null : identityClaim("qianchuan_account_id", ["account_id", "accountId"], ["data-account-id"], ["account_id", "accountId", "selected_account_id"]);
    const values = [store, advertiser, account].filter(Boolean);
    return {
      claims: values.filter((item) => item.raw_id),
      status: values.some((item) => item.conflict) ? "conflict" : values.some((item) => item.raw_id) ? "resolved_by_bridge" : "unresolved",
    };
  }

  function detectPageType() {
    const path = location.pathname.toLowerCase();
    const pageText = document.body?.innerText || "";
    const activeTab = Array.from(document.querySelectorAll("[role='tab'][aria-selected='true'], [class*='tab'][class*='active']"))
      .map((element) => (element.innerText || "").trim()).join(" ");
    if (path.includes("video-library") || /视频库/.test(document.title)) return "video_library";
    if (path.includes("material") || path.includes("creative")) return "materials";
    if (path.includes("board-next") || /直播大屏/.test(document.title)) return "live_dashboard";
    if (/商品/.test(activeTab) && /推广/.test(activeTab)) return "campaigns";
    if (/直播/.test(activeTab) && /推广/.test(activeTab)) return "qianchuan_live";
    if (/设置直播规划/.test(pageText)) return "qianchuan_live";
    if (path.includes("live") || path.includes("screen")) return "qianchuan_live";
    if (path === "/home" || path.endsWith("/home")) return "overview";
    if (path.includes("uni-prom") || path.includes("promotion") || path.includes("manage")) return "campaigns";
    if (path.includes("report") || path.includes("data")) return "report";
    if (path.includes("account") || path.includes("fund")) return "account";
    if (location.hostname.includes("buyin")) return "affiliate";
    return "unknown";
  }

  function detectPromotionContext() {
    const visibleText = `${document.title || ""}\n${document.body?.innerText || ""}`.slice(0, 120000);
    const matched = [
      ["chengfang", "乘方"],
      ["full_domain", "全域推广"],
      ["standard", "标准推广"],
    ].filter(([, label]) => visibleText.includes(label));
    const promotionMode = matched.length === 1 ? matched[0][0] : "unknown";
    return {
      schema_version: 1,
      promotion_mode: promotionMode,
      strategy_id: "",
      metric: { definition: "unknown", label: "", value: null, period: "" },
      cost_ledger: {},
      platform_managed_fields: promotionMode === "chengfang" ? ["预算分配", "流量分配", "素材协同"] : [],
      evidence: {
        source: matched.length === 1 ? "visible_label" : matched.length > 1 ? "conflicting_visible_labels" : "unverified",
        label: matched.length === 1 ? matched[0][1] : "",
        captured_at_ms: Date.now(),
      },
    };
  }

  function assertLegacyExecutionMode(request) {
    const mode = String(request?.promotion_context?.promotion_mode || "unknown");
    if (mode === "chengfang") throw new Error("UNSUPPORTED_FOR_CHENGFANG：乘方模式禁止使用旧单计划预算、暂停和恢复执行器。");
    if (!["standard", "full_domain"].includes(mode)) throw new Error("PROMOTION_MODE_UNVERIFIED：尚未确认当前投放模式，已停止执行。");
  }

  async function capture(reason = "auto") {
    const stored = await chrome.storage.local.get("settings");
    const privacyMode = stored.settings?.privacyMode !== false;
    const data = await globalThis.DianAgentExtractor.collect(SOURCE, detectPageType(), privacyMode, reason);
    data.promotion_context = detectPromotionContext();
    const identity = detectIdentityClaims();
    data.identity_claims = identity.claims;
    data.identity_status = identity.status;
    const response = await chrome.runtime.sendMessage({ type: "page-data", source: SOURCE, data });
    return { ok: true, page_type: data.page_type, quality: data.quality, account: response?.account || null, store: response?.store || null, bridge: response };
  }

  async function ensureResolvedAccount(request) {
    const resolved = await capture("execution-identity-check");
    const account = resolved.account;
    if (!account || account.key !== request.account_key) throw new Error("当前千川账号与授权账号不一致。");
    return account;
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

  function rowHasExactPlanId(rowText, planId) {
    const target = String(planId || "").normalize("NFKC").trim().toLowerCase();
    if (!target || !/^[a-z0-9_-]+$/.test(target)) return false;
    const pseudonymized = globalThis.DianAgentExtractor.pseudonymizePlanIdentifier(rowText, "计划");
    return normalizedPlanIdTokens(rowText).includes(target)
      || normalizedPlanIdTokens(pseudonymized).includes(target);
  }

  async function waitFor(check, timeoutMs = 8000, intervalMs = 200) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const value = check();
      if (value) return value;
      await new Promise((resolve) => setTimeout(resolve, intervalMs));
    }
    return null;
  }

  async function probeBudgetExecution(request) {
    assertLegacyExecutionMode(request);
    if (!["adjust_budget", "restore_budget"].includes(request?.operation_type) || request?.mode !== "supervised_submit") {
      throw new Error("当前页面执行器只支持受监督降低预算或恢复原预算。");
    }
    await ensureResolvedAccount(request);
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
      return rowHasExactPlanId(text, planId) && text.includes(planName);
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
    return { ok: true, ready: true, plan_id: planId, current_value: expected, target_value: target };
  }

  function findPlanRow(request) {
    const planId = String(request.plan_id || "").trim();
    const planName = String(request.plan_name || "").trim();
    const rows = Array.from(document.querySelectorAll("tr, [role='row'], [class*='table-row'], [class*='TableRow']")).filter(visible);
    const matches = rows.filter((row) => {
      const text = String(row.innerText || "").replace(/\s+/g, " ");
      return rowHasExactPlanId(text, planId) && text.includes(planName);
    });
    if (matches.length !== 1) throw new Error(matches.length ? "页面存在多个同名计划，已停止执行。" : "当前页面未找到授权计划，请打开对应计划列表。");
    return matches[0];
  }

  function statusText(row) {
    return String(row.innerText || "").replace(/\s+/g, " ").trim();
  }

  async function pausePlanProbe(request) {
    assertLegacyExecutionMode(request);
    if (request?.operation_type !== "pause_plan" || request?.mode !== "supervised_submit") throw new Error("当前页面不是受监督暂停动作。");
    await ensureResolvedAccount(request);
    if (String(request.expected_current_value || "") !== "投放中" && !["启用", "生效中", "运行中"].includes(String(request.expected_current_value || ""))) throw new Error("当前计划状态不是可暂停状态。");
    if (String(request.target_value || "") !== "暂停") throw new Error("暂停目标无效。");
    const row = findPlanRow(request);
    const buttons = Array.from(row.querySelectorAll("button, [role='button'], [role='switch']")).filter((button) => visible(button) && !button.disabled && /^(暂停|停用)$/.test(String(button.innerText || button.textContent || "").trim()));
    if (buttons.length !== 1) throw new Error(buttons.length ? "发现多个暂停按钮，已停止执行。" : "未找到唯一暂停按钮。");
    return { ok: true, ready: true, plan_id: request.plan_id, status: statusText(row) };
  }

  async function supervisedPauseSubmit(request) {
    await pausePlanProbe(request);
    const row = findPlanRow(request);
    const button = Array.from(row.querySelectorAll("button, [role='button'], [role='switch']")).filter((item) => visible(item) && !item.disabled && /^(暂停|停用)$/.test(String(item.innerText || item.textContent || "").trim()))[0];
    if (!button) throw new Error("未找到唯一暂停按钮，页面未做任何修改。");
    button.click();
    const success = await waitFor(() => {
      const text = statusText(findPlanRow(request));
      return /(?:已暂停|暂停中)(?:\s|$)/.test(text) || /(^|\s)暂停(\s|$)/.test(text);
    }, 10000);
    if (!success) throw new Error("已点击暂停，但未读取到暂停状态；请立即在千川核对，系统不会重复提交。");
    return { ok: true, mode: "supervised_submit", plan_id: request.plan_id, target_value: "暂停", submitted: true, platform_success_observed: true };
  }

  async function supervisedBudgetSubmit(request) {
    await probeBudgetExecution(request);
    if (!["adjust_budget", "restore_budget"].includes(request?.operation_type) || request?.mode !== "supervised_submit") {
      throw new Error("当前页面执行器只支持受监督降低预算或恢复原预算。");
    }
    const planId = String(request.plan_id || "").trim();
    const planName = String(request.plan_name || "").trim();
    if (!planId || !planName) throw new Error("授权缺少计划唯一 ID 或名称。");
    const rows = Array.from(document.querySelectorAll("tr, [role='row'], [class*='table-row'], [class*='TableRow']"))
      .filter((row) => row.getClientRects().length > 0);
    const matches = rows.filter((row) => {
      const text = String(row.innerText || "").replace(/\s+/g, " ");
      return rowHasExactPlanId(text, planId) && text.includes(planName);
    });
    if (matches.length !== 1) throw new Error(matches.length ? "页面存在多个同名计划，已停止辅助填写。" : "当前页面未找到授权计划，请打开对应计划列表。");
    const row = matches[0];
    const inputs = Array.from(row.querySelectorAll("input")).filter((input) => input.getClientRects().length > 0 && !input.disabled);
    const expected = Number(request.expected_current_value);
    const budgetInput = inputs.find((input) => {
      const label = `${input.getAttribute("aria-label") || ""} ${input.getAttribute("placeholder") || ""}`;
      return /预算/.test(label) && Math.abs((normalizedNumber(input.value) ?? NaN) - expected) <= 0.01;
    });
    if (!budgetInput) throw new Error("未找到与授权当前预算一致的输入框，页面未做任何修改。");
    const target = Number(request.target_value);
    const inRange = request.operation_type === "restore_budget"
      ? target > expected && (target - expected) / expected <= 0.50
      : target > 0 && target < expected && (expected - target) / expected <= 0.30;
    if (!Number.isFinite(target) || !inRange) {
      throw new Error("目标预算不符合首批止损或回滚范围。");
    }
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    if (!setter) throw new Error("浏览器不支持安全填写预算。");
    setter.call(budgetInput, String(target));
    budgetInput.dispatchEvent(new Event("input", { bubbles: true }));
    budgetInput.dispatchEvent(new Event("change", { bubbles: true }));
    budgetInput.focus();
    budgetInput.style.outline = "3px solid #f59e0b";
    budgetInput.scrollIntoView({ behavior: "smooth", block: "center" });

    const submitScope = budgetInput.closest("[role='dialog'], [class*='modal'], [class*='drawer']") || row;
    const buttons = Array.from(submitScope.querySelectorAll("button")).filter(visible);
    const submitButtons = buttons.filter((button) => /^(确认|确定|保存|提交)$/.test(String(button.innerText || button.textContent || "").trim())
      && !button.disabled && button.getAttribute("aria-disabled") !== "true");
    if (submitButtons.length !== 1) {
      setter.call(budgetInput, String(expected));
      budgetInput.dispatchEvent(new Event("input", { bubbles: true }));
      throw new Error(submitButtons.length ? "发现多个提交按钮，已恢复原预算并停止。" : "未找到唯一提交按钮，已恢复原预算并停止。");
    }
    submitButtons[0].click();
    const successObserved = await waitFor(() => {
      const notices = Array.from(document.querySelectorAll(
        "[role='alert'], [class*='toast'], [class*='message'], [class*='notification']",
      )).filter(visible);
      return notices.some((item) => /成功|已保存|修改完成|设置完成/.test(String(item.innerText || item.textContent || "")));
    });
    if (!successObserved) {
      throw new Error("已点击平台提交，但未读取到成功回执；请立即在千川核对，系统不会重复提交。");
    }
    return {
      ok: true,
      mode: "supervised_submit",
      plan_id: planId,
      target_value: target,
      submitted: true,
      platform_success_observed: true,
    };
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message.type === "collect-now") {
      capture(message.reason || "manual")
        .then(sendResponse)
        .catch((error) => sendResponse({ ok: false, error: error.message || String(error) }));
      return true;
    }
    if (message.type === "qianchuan-supervised-submit") {
      (message.request?.operation_type === "pause_plan" ? supervisedPauseSubmit(message.request) : supervisedBudgetSubmit(message.request))
        .then(sendResponse)
        .catch((error) => sendResponse({ ok: false, error: error.message || String(error), submitted: false }));
      return true;
    }
    if (message.type === "qianchuan-execution-probe") {
      (message.request?.operation_type === "pause_plan" ? pausePlanProbe(message.request) : probeBudgetExecution(message.request))
        .then(sendResponse)
        .catch((error) => sendResponse({ ok: false, ready: false, error: error.message || String(error) }));
      return true;
    }
    return false;
  });

  setTimeout(() => capture("page-load").catch(() => {}), RENDER_DELAY);
  const observer = new MutationObserver(() => {
    if (location.href === lastUrl) return;
    lastUrl = location.href;
    clearTimeout(routeTimer);
    routeTimer = setTimeout(() => capture("route-change").catch(() => {}), RENDER_DELAY);
  });
  observer.observe(document.documentElement, { childList: true, subtree: true });
})();
