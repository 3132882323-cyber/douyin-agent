/** 抖店页面采集器 */
(function () {
  "use strict";
  if (globalThis.__DianAgentDoudianLoaded) return;
  globalThis.__DianAgentDoudianLoaded = true;
  const SOURCE = "doudian";
  const RENDER_DELAY = 2600;
  let lastUrl = location.href;
  let routeTimer = null;

  function detectPageType() {
    const path = location.pathname.toLowerCase();
    if (path.includes("/ad/promotion-v2")) {
      const activeTab = document.querySelector("[role='tab'][aria-selected='true'], .aurora-qc-tabs-tab-active, [class*='tabs-tab-active']");
      const activeText = activeTab?.innerText || "";
      if (activeText.includes("直播")) return "qianchuan_live";
      if (activeText.includes("数据")) return "qianchuan_report";
      return "qianchuan_campaigns";
    }
    if (path.includes("growth-shelf")) return "shelf";
    if (path.includes("short-video")) return "short_video";
    if (path.includes("image-text")) return "image_text";
    if (path.includes("recommend-card")) return "recommend_card";
    if (path.includes("mshop/homepage")) return "overview";
    if (path.includes("morder/order")) return "orders";
    if (path.includes("comment") || path.includes("review")) return "reviews";
    if (path.includes("aftersale") || path.includes("refund")) return "refunds";
    if (path.includes("/g/list") || path.includes("goods") || path.includes("product")) return "products";
    if (path.includes("stock")) return "inventory";
    if (path.includes("shop-live") || path.includes("live")) return "live";
    if (path.includes("compass") || path.includes("mcompass")) return "search";
    if (path.includes("fund") || path.includes("account-center")) return "funds";
    return "unknown";
  }

  async function capture(reason = "auto") {
    const stored = await chrome.storage.local.get("settings");
    const privacyMode = stored.settings?.privacyMode !== false;
    const data = await globalThis.DianAgentExtractor.collect(SOURCE, detectPageType(), privacyMode, reason);
    const response = await chrome.runtime.sendMessage({ type: "page-data", source: SOURCE, data });
    return { ok: true, page_type: data.page_type, quality: data.quality, bridge: response };
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
