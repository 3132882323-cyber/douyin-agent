/** 巨量千川页面采集器 */
(function () {
  "use strict";
  if (globalThis.__DianAgentQianchuanLoaded) return;
  globalThis.__DianAgentQianchuanLoaded = true;
  const SOURCE = "qianchuan";
  const RENDER_DELAY = 3200;
  let lastUrl = location.href;
  let routeTimer = null;

  function accountHash(value) {
    let hash = 2166136261;
    for (const character of String(value || "")) {
      hash ^= character.charCodeAt(0);
      hash = Math.imul(hash, 16777619);
    }
    return `acct_${(hash >>> 0).toString(16).padStart(8, "0")}`;
  }

  function normalizeAccountLabel(value) {
    const label = String(value || "")
      .replace(/\u200b/g, "")
      .replace(/\s+/g, " ")
      .replace(/^(?:当前账号|账号名称|千川账号|店铺名称)\s*[:：]?\s*/i, "")
      .trim();
    if (label.length < 2 || label.length > 48) return "";
    if (/^(?:店铺|账号|账户|广告主|千川|巨量千川|全部账号|切换账号|账号管理|ID|ID[:：])$/i.test(label)) return "";
    if (/(?:我的资金|账户明细|账户余额|活动福利|福利明细|立即充值|消息中心|帮助中心|切换账号|账号管理|全部账号)/.test(label)) return "";
    if (/^(?:ID|账号ID|账户ID|店铺ID)\s*[:：]?\s*$/i.test(label)) return "";
    return label;
  }

  function detectStoredAccountId() {
    const idKeyPattern = /(?:advertiser(?:[_-]?id|Id)|aadvid|advid|account(?:[_-]?id|Id)|shop(?:[_-]?id|Id))/i;
    const jsonIdPattern = /"(?:advertiser_id|advertiserId|aadvid|advid|account_id|accountId|shop_id|shopId)"\s*:\s*"?([A-Za-z0-9_-]{4,64})"?/;
    for (const storage of [globalThis.sessionStorage, globalThis.localStorage]) {
      if (!storage) continue;
      try {
        for (let index = 0; index < Math.min(storage.length, 120); index += 1) {
          const key = String(storage.key(index) || "");
          const value = String(storage.getItem(key) || "").slice(0, 4096);
          if (idKeyPattern.test(key) && /^[A-Za-z0-9_-]{4,64}$/.test(value)) return value;
          const match = value.match(jsonIdPattern);
          if (match?.[1]) return match[1];
        }
      } catch {
        // Storage access can be blocked by browser policy on some routes.
      }
    }
    return "";
  }

  function detectAccountContext() {
    if (location.pathname === "/login" || location.pathname.startsWith("/login/")) return null;
    const searchParams = new URLSearchParams(location.search);
    const hashSearch = String(location.hash || "").includes("?")
      ? String(location.hash).slice(String(location.hash).indexOf("?"))
      : "";
    const hashParams = new URLSearchParams(hashSearch);
    const pageText = (document.body?.innerText || "").slice(0, 12000);
    const idKeys = ["advertiser_id", "advertiserId", "aadvid", "advid", "adv_id", "account_id", "accountId", "shop_id", "shopId"];
    const queryAccountId = idKeys
      .flatMap((key) => [searchParams.get(key), hashParams.get(key)])
      .find((value) => value && /^[A-Za-z0-9_-]{4,64}$/.test(value));
    const attributeAccountId = Array.from(document.querySelectorAll(
      "[data-advertiser-id], [data-account-id], [data-shop-id], [data-aadvid]",
    )).map((element) => (
      element.getAttribute("data-advertiser-id")
      || element.getAttribute("data-account-id")
      || element.getAttribute("data-shop-id")
      || element.getAttribute("data-aadvid")
    )).find((value) => value && /^[A-Za-z0-9_-]{4,64}$/.test(value));
    const textAccountId = pageText.match(/(?:广告主|账户|账号|店铺)\s*(?:ID|id|编号)\s*[:：]?\s*([A-Za-z0-9_-]{4,64})/)?.[1] || "";
    const accountId = queryAccountId || attributeAccountId || detectStoredAccountId() || textAccountId;
    const selectors = [
      "[data-testid*='account-name']", "[data-testid*='shop-name']", "[data-testid*='advertiser-name']",
      "[class*='accountName']", "[class*='advertiserName']", "[class*='shopName']",
      "[class*='account-name']", "[class*='advertiser-name']", "[class*='shop-name']",
      "[class*='account'] [class*='name']", "[class*='header'] [class*='account']",
    ];
    let label = "";
    for (const selector of selectors) {
      const elements = Array.from(document.querySelectorAll(selector)).filter((item) => item.getClientRects().length > 0);
      const value = elements.map((element) => normalizeAccountLabel(element.innerText)).find(Boolean);
      if (value) {
        label = value;
        break;
      }
    }
    if (!label) {
      const match = pageText.match(/(?:当前账号|账号名称|千川账号|店铺名称)\s*[:：]?\s*\n?\s*([^\n]{2,80})/);
      label = normalizeAccountLabel(match?.[1]);
    }
    if (!accountId && !label) return null;
    // A platform account ID is the only safe discriminator when several
    // Qianchuan accounts share the same visible shop name.
    const identity = accountId || label;
    return {
      key: accountHash(identity),
      label: label || `千川账号 · ${String(accountId).slice(-4)}`,
      confidence: accountId ? "high" : "medium",
      identity_source: accountId ? "platform_id" : "account_label",
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

  async function capture(reason = "auto") {
    const stored = await chrome.storage.local.get("settings");
    const privacyMode = stored.settings?.privacyMode !== false;
    const maxDeepScanPages = Math.max(1, Math.min(20, Number(stored.settings?.maxDeepScanPages) || 5));
    const data = await globalThis.DianAgentExtractor.collect(SOURCE, detectPageType(), privacyMode, reason, { maxDeepScanPages });
    data.account = detectAccountContext();
    const response = await chrome.runtime.sendMessage({ type: "page-data", source: SOURCE, data });
    return { ok: true, page_type: data.page_type, quality: data.quality, account: data.account, bridge: response };
  }

  function normalizedNumber(value) {
    const parsed = Number(String(value ?? "").replace(/[^\d.-]/g, ""));
    return Number.isFinite(parsed) ? parsed : null;
  }

  function visible(element) {
    return Boolean(element && element.getClientRects().length > 0);
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

  function findAuthorizedPlanRow(request) {
    const planId = String(request.plan_id || "").trim();
    const planName = String(request.plan_name || "").trim();
    if (!planId || !planName) throw new Error("授权缺少计划唯一 ID 或名称。");
    const rows = Array.from(document.querySelectorAll("tr, [role='row'], [class*='table-row'], [class*='TableRow']"))
      .filter(visible);
    const matches = rows.filter((row) => {
      const text = String(row.innerText || "").replace(/\s+/g, " ");
      const pseudonymized = globalThis.DianAgentExtractor.pseudonymizePlanIdentifier(text, "计划");
      return (text.includes(planId) || pseudonymized.includes(planId)) && text.includes(planName);
    });
    if (matches.length !== 1) throw new Error(matches.length ? "页面存在多个同名计划，已停止执行。" : "当前页面未找到授权计划，请打开对应计划列表。");
    return matches[0];
  }

  function findPauseControl(row, expectedStatus) {
    const switches = Array.from(row.querySelectorAll("button, [role='switch'], input[type='checkbox']")).filter((node) => {
      if (!visible(node) || node.disabled || node.getAttribute("aria-disabled") === "true") return false;
      const label = `${node.getAttribute("aria-label") || ""} ${node.innerText || node.textContent || ""}`;
      const pressed = node.getAttribute("aria-checked") === "true" || node.getAttribute("aria-pressed") === "true" || node.checked === true;
      if (/暂停|停用/.test(label)) return true;
      if (/启用|投放|开启/.test(label) && pressed) return true;
      if ((node.getAttribute("role") === "switch" || node.type === "checkbox") && pressed) return true;
      return false;
    });
    if (expectedStatus && !String(row.innerText || "").includes(expectedStatus)) {
      throw new Error("当前计划投放状态与授权不一致。");
    }
    if (switches.length !== 1) {
      throw new Error(switches.length ? "发现多个启停控件，已停止执行。" : "未找到唯一暂停控件。");
    }
    return switches[0];
  }

  function probePauseExecution(request) {
    if (request?.operation_type !== "pause_plan" || request?.mode !== "supervised_submit") {
      throw new Error("当前页面执行器只支持受监督单计划暂停。");
    }
    const account = detectAccountContext();
    if (!account || account.key !== request.account_key) throw new Error("当前千川账号与授权账号不一致。");
    if (String(request.target_value || "") !== "暂停") throw new Error("暂停目标状态无效。");
    const expected = String(request.expected_current_value || "");
    if (!["投放中", "启用", "生效中", "运行中"].includes(expected)) {
      throw new Error("授权缺少已投放状态。");
    }
    const row = findAuthorizedPlanRow(request);
    findPauseControl(row, expected);
    return { ok: true, ready: true, plan_id: String(request.plan_id || ""), current_value: expected, target_value: "暂停" };
  }

  async function supervisedPauseSubmit(request) {
    probePauseExecution(request);
    const account = detectAccountContext();
    if (!account || account.key !== request.account_key) throw new Error("当前千川账号与授权账号不一致。");
    const row = findAuthorizedPlanRow(request);
    const control = findPauseControl(row, String(request.expected_current_value || ""));
    control.click();
    const successObserved = await waitFor(() => {
      const notices = Array.from(document.querySelectorAll(
        "[role='alert'], [class*='toast'], [class*='message'], [class*='notification']",
      )).filter(visible);
      return notices.some((item) => /成功|已保存|已暂停|修改完成|设置完成/.test(String(item.innerText || item.textContent || "")));
    });
    if (!successObserved) {
      throw new Error("已点击平台暂停，但未读取到成功回执；请立即在千川核对，系统不会重复提交。");
    }
    return {
      ok: true,
      mode: "supervised_submit",
      plan_id: String(request.plan_id || ""),
      target_value: "暂停",
      submitted: true,
      platform_success_observed: true,
    };
  }

  function probeBudgetExecution(request) {
    if (request?.operation_type === "pause_plan") return probePauseExecution(request);
    if (!["adjust_budget", "restore_budget"].includes(request?.operation_type) || request?.mode !== "supervised_submit") {
      throw new Error("当前页面执行器只支持受监督降低预算、恢复原预算或单计划暂停。");
    }
    const account = detectAccountContext();
    if (!account || account.key !== request.account_key) throw new Error("当前千川账号与授权账号不一致。");
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
      const pseudonymized = globalThis.DianAgentExtractor.pseudonymizePlanIdentifier(text, "计划");
      return (text.includes(planId) || pseudonymized.includes(planId)) && text.includes(planName);
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

  async function supervisedBudgetSubmit(request) {
    if (request?.operation_type === "pause_plan") return supervisedPauseSubmit(request);
    probeBudgetExecution(request);
    if (!["adjust_budget", "restore_budget"].includes(request?.operation_type) || request?.mode !== "supervised_submit") {
      throw new Error("当前页面执行器只支持受监督降低预算、恢复原预算或单计划暂停。");
    }
    const account = detectAccountContext();
    if (!account || account.key !== request.account_key) throw new Error("当前千川账号与授权账号不一致。");
    const planId = String(request.plan_id || "").trim();
    const planName = String(request.plan_name || "").trim();
    if (!planId || !planName) throw new Error("授权缺少计划唯一 ID 或名称。");
    const rows = Array.from(document.querySelectorAll("tr, [role='row'], [class*='table-row'], [class*='TableRow']"))
      .filter((row) => row.getClientRects().length > 0);
    const matches = rows.filter((row) => {
      const text = String(row.innerText || "").replace(/\s+/g, " ");
      const pseudonymized = globalThis.DianAgentExtractor.pseudonymizePlanIdentifier(text, "计划");
      return (text.includes(planId) || pseudonymized.includes(planId)) && text.includes(planName);
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
      supervisedBudgetSubmit(message.request)
        .then(sendResponse)
        .catch((error) => sendResponse({ ok: false, error: error.message || String(error), submitted: false }));
      return true;
    }
    if (message.type === "qianchuan-execution-probe") {
      try {
        sendResponse(probeBudgetExecution(message.request));
      } catch (error) {
        sendResponse({ ok: false, ready: false, error: error.message || String(error) });
      }
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
