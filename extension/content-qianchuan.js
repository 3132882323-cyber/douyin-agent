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

  function detectAccountContext() {
    const params = new URLSearchParams(location.search);
    const pageText = (document.body?.innerText || "").slice(0, 12000);
    const queryAccountId = ["advertiser_id", "aadvid", "account_id", "shop_id"]
      .map((key) => params.get(key)).find((value) => value && /^[A-Za-z0-9_-]{4,64}$/.test(value));
    const textAccountId = pageText.match(/(?:广告主|账户|账号|店铺)\s*(?:ID|id|编号)\s*[:：]?\s*([A-Za-z0-9_-]{4,64})/)?.[1] || "";
    const accountId = queryAccountId || textAccountId;
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
    // Prefer the visible account label when available. Some Qianchuan routes
    // expose an account ID while others omit it; switching identity sources
    // between pages would otherwise create a different key mid-scan.
    const identity = label || accountId;
    return {
      key: accountHash(identity),
      label: label || `千川账号 · ${String(accountId).slice(-4)}`,
      confidence: label && accountId ? "high" : "medium",
      identity_source: label ? "account_label" : "platform_id",
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
    const data = await globalThis.DianAgentExtractor.collect(SOURCE, detectPageType(), privacyMode, reason);
    data.account = detectAccountContext();
    const response = await chrome.runtime.sendMessage({ type: "page-data", source: SOURCE, data });
    return { ok: true, page_type: data.page_type, quality: data.quality, account: data.account, bridge: response };
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message.type !== "collect-now") return false;
    capture(message.reason || "manual")
      .then(sendResponse)
      .catch((error) => sendResponse({ ok: false, error: error.message || String(error) }));
    return true;
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
