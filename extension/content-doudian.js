/** 抖店页面采集器 */
(function () {
  "use strict";
  if (globalThis.__DianAgentDoudianLoaded) return;
  globalThis.__DianAgentDoudianLoaded = true;
  const SOURCE = "doudian";
  const RENDER_DELAY = 2600;
  let lastUrl = location.href;
  let routeTimer = null;

  function detectStoredShopId() {
    const keyPattern = /(?:shop|store)(?:[_-]?id|Id)/i;
    const jsonPattern = /"(?:shop_id|shopId|store_id|storeId)"\s*:\s*"?([A-Za-z0-9_-]{4,80})"?/;
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
        // Some routes deny access to one of the storage areas.
      }
    }
    return [...new Set(found)];
  }

  function detectShopIdentityClaim() {
    const searchParams = new URLSearchParams(location.search || "");
    const hashSearch = String(location.hash || "").includes("?")
      ? String(location.hash).slice(String(location.hash).indexOf("?"))
      : "";
    const hashParams = new URLSearchParams(hashSearch);
    const keys = ["shop_id", "shopId", "store_id", "storeId"];
    const queryIds = keys.flatMap((key) => [searchParams.get(key), hashParams.get(key)])
      .filter((value) => value && /^[A-Za-z0-9_-]{4,80}$/.test(value));
    const attributeIds = Array.from(document.querySelectorAll("[data-shop-id], [data-store-id]"))
      .map((element) => element.getAttribute("data-shop-id") || element.getAttribute("data-store-id"))
      .filter((value) => value && /^[A-Za-z0-9_-]{4,80}$/.test(value));
    const highCandidates = [...new Set([...queryIds, ...attributeIds])];
    if (highCandidates.length > 1) return { conflict: true, confidence: "conflict" };
    if (highCandidates.length === 1) {
      return {
        kind: "douyin_shop_id",
        raw_id: highCandidates[0],
        evidence_source: queryIds.includes(highCandidates[0]) ? "url_parameter" : "data_attribute",
        confidence: "high",
      };
    }
    const storedIds = detectStoredShopId();
    if (storedIds.length > 1) return { conflict: true, confidence: "conflict" };
    return storedIds.length === 1 ? { kind: "douyin_shop_id", raw_id: storedIds[0], evidence_source: "allowlisted_storage", confidence: "medium" } : null;
  }

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
    const identityClaim = location.pathname === "/login" || location.pathname.startsWith("/login/")
      ? null
      : detectShopIdentityClaim();
    data.identity_claims = identityClaim?.raw_id ? [identityClaim] : [];
    data.identity_status = identityClaim?.conflict ? "conflict" : identityClaim ? "resolved_by_bridge" : "unresolved";
    const response = await chrome.runtime.sendMessage({ type: "page-data", source: SOURCE, data });
    return { ok: true, page_type: data.page_type, quality: data.quality, store: response?.store || null, bridge: response };
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
