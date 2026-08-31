/** 抖店页面采集器 */
(function () {
  "use strict";
  if (globalThis.__DianAgentDoudianLoaded) return;
  globalThis.__DianAgentDoudianLoaded = true;
  const SOURCE = "doudian";
  const RUNTIME_VERSION = String(chrome.runtime.getManifest?.().version || "");
  globalThis.__DianAgentDoudianRuntimeVersion = RUNTIME_VERSION;
  let collectionContextEpoch = 0;
  let lastCollectionContextFingerprint = "";
  let contextObserverStarted = false;
  function detectBootstrapShopIds() {
    // The current Doudian shell embeds one active shop context in its inline
    // bootstrap payload even when the visible header no longer prints an ID.
    // Read only the exact sec_shop_id field, cap work, and accept it only when
    // the complete document yields one distinct value. Lists or mixed values
    // remain unresolved instead of being guessed.
    const found = [];
    let remaining = 2_000_000;
    for (const script of Array.from(document.scripts || []).slice(0, 80)) {
      if (remaining <= 0) break;
      if (String(script?.src || "")) continue;
      const text = String(script?.textContent || "").slice(0, remaining);
      remaining -= text.length;
      const pattern = /["']sec_shop_id["']\s*:\s*(?:["']([A-Za-z0-9_-]{4,80})["']|(\d{4,80}))/gi;
      for (const match of text.matchAll(pattern)) found.push(String(match[1] || match[2] || ""));
    }
    return [...new Set(found.filter((value) => /^[A-Za-z0-9_-]{4,80}$/.test(value)))];
  }

  function detectStoredShopId() {
    // Only treat a scalar, active identity key as evidence. Platform storage
    // often contains historical shop lists; folding every cached shop id into
    // one page identity made normal multi-shop operators look "conflicted".
    // Generic shopId/shopList cache entries can describe the last visited or
    // every historical shop. Only an explicitly current/active scalar key may
    // participate in bootstrap identity evidence.
    const keyPattern = /^(?:current|active)[_-]?(?:shop|store)(?:[_-]?id)$/i;
    const found = [];
    for (const storage of [globalThis.sessionStorage, globalThis.localStorage]) {
      if (!storage) continue;
      try {
        for (let index = 0; index < Math.min(storage.length, 120); index += 1) {
          const key = String(storage.key(index) || "");
          const value = String(storage.getItem(key) || "").slice(0, 4096);
          if (keyPattern.test(key) && /^[A-Za-z0-9_-]{4,80}$/.test(value)) found.push(value);
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
    const overviewText = location.pathname.toLowerCase().includes("/mshop/homepage")
      ? String(document.body?.innerText || "").slice(0, 120000)
      : "";
    const visibleMatch = overviewText.match(/(?:店铺|商家)\s*(?:ID|id|编号)\s*[:：]?\s*([A-Za-z0-9_-]{4,80})/);
    const visibleIds = visibleMatch?.[1] ? [visibleMatch[1]] : [];
    const bootstrapIds = detectBootstrapShopIds();
    const activeBootstrapIds = bootstrapIds.length === 1 ? bootstrapIds : [];
    const highCandidates = [...new Set([...queryIds, ...attributeIds, ...visibleIds, ...activeBootstrapIds])];
    if (highCandidates.length > 1) return { conflict: true, kind: "douyin_shop_id", confidence: "conflict" };
    if (highCandidates.length === 1) {
      return {
        kind: "douyin_shop_id",
        raw_id: highCandidates[0],
        evidence_source: queryIds.includes(highCandidates[0])
          ? "url_parameter"
          : attributeIds.includes(highCandidates[0])
            ? "data_attribute"
            : visibleIds.includes(highCandidates[0]) ? "overview_visible_text" : "bootstrap_sec_shop_id",
        confidence: "high",
      };
    }
    const storedIds = detectStoredShopId();
    // Multiple medium-confidence cache values are ambiguity, not proof that
    // the visible page itself contains conflicting high-confidence identities.
    // Leave the page unresolved so an explicitly confirmed scan session can
    // bind it without mixing passive page-load data across stores.
    if (storedIds.length > 1) return null;
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

  function currentCollectionContextFingerprint() {
    const identity = detectShopIdentityClaim();
    return JSON.stringify({
      href: String(location.href || `${location.pathname || ""}${location.search || ""}${location.hash || ""}`),
      page_type: detectPageType(),
      identity: identity ? {
        kind: String(identity.kind || ""),
        raw_id: String(identity.raw_id || ""),
        conflict: identity.conflict === true,
      } : null,
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
    ensureContextObserver();
    const entryEpoch = sampleCollectionContext();
    const assertContextStable = () => {
      sampleCollectionContext();
      if (collectionContextEpoch !== entryEpoch) {
        const error = new Error("COLLECTION_CONTEXT_CHANGED: 采集期间抖店页面发生变化，本次数据已丢弃，请重新巡店。");
        error.code = "COLLECTION_CONTEXT_CHANGED";
        throw error;
      }
    };
    const stored = await chrome.storage.local.get("settings");
    assertContextStable();
    const privacyMode = stored.settings?.privacyMode !== false;
    const data = await globalThis.DianAgentExtractor.collect(
      SOURCE,
      detectPageType(),
      privacyMode,
      reason,
      { assertContextStable },
    );
    assertContextStable();
    const identityClaim = location.pathname === "/login" || location.pathname.startsWith("/login/")
      ? null
      : detectShopIdentityClaim();
    data.identity_claims = identityClaim?.raw_id ? [identityClaim] : [];
    data.identity_status = identityClaim?.conflict ? "conflict" : identityClaim ? "resolved_by_bridge" : "unresolved";
    data.identity_conflicts = identityClaim?.conflict ? [identityClaim.kind || "douyin_shop_id"] : [];
    if (options.deferPush === true) {
      assertContextStable();
      return { ok: true, page_type: data.page_type, quality: data.quality, snapshot: data };
    }
    assertContextStable();
    const response = await chrome.runtime.sendMessage({ type: "page-data", source: SOURCE, data });
    assertContextStable();
    return { ok: true, page_type: data.page_type, quality: data.quality, store: response?.store || null, bridge: response };
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message.type === "dian-agent-content-status") {
      sendResponse({ ok: true, source: SOURCE, runtime_version: RUNTIME_VERSION, page_type: detectPageType() });
      return false;
    }
    if (message.type !== "collect-now") return false;
    capture(message.reason || "manual", { deferPush: message.defer_push === true })
      .then(sendResponse)
      .catch((error) => sendResponse({
        ok: false,
        error: error.message || String(error),
        code: error?.code || "",
        error_code: error?.code || "",
      }));
    return true;
  });

  // Collection is admission-controlled by the service worker. Background
  // alarms already implement opt-in auto sync, so page-load/route observers
  // would only duplicate snapshots and race full-scan validation.
})();
