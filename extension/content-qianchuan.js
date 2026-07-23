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

  function detectAccountContext() {
    const params = new URLSearchParams(location.search);
    const accountId = ["advertiser_id", "aadvid", "account_id", "shop_id"]
      .map((key) => params.get(key)).find((value) => value && /^[A-Za-z0-9_-]{4,64}$/.test(value));
    const selectors = [
      "[class*='account-name']", "[class*='advertiser-name']", "[class*='shop-name']",
      "[class*='account'] [class*='name']", "[class*='header'] [class*='account']",
    ];
    let label = "";
    for (const selector of selectors) {
      const element = Array.from(document.querySelectorAll(selector)).find((item) => item.getClientRects().length > 0);
      const value = (element?.innerText || "").replace(/\s+/g, " ").trim();
      if (value.length >= 2 && value.length <= 80 && !/切换账号|账号管理|全部账号/.test(value)) {
        label = value;
        break;
      }
    }
    if (!label) {
      const text = (document.body?.innerText || "").slice(0, 5000);
      const match = text.match(/(?:当前账号|账号名称|千川账号|店铺名称)\s*[:：]?\s*\n?\s*([^\n]{2,80})/);
      if (match && !/切换账号|账号管理|全部账号/.test(match[1])) label = match[1].trim();
    }
    if (!accountId && !label) return null;
    return {
      key: accountHash(accountId || label),
      label: label || `千川账号 · ${String(accountId).slice(-4)}`,
      confidence: label && accountId ? "high" : "medium",
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
