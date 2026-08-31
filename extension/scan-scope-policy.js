/** 店策 Agent - 巡店范围单一契约 */
(function () {
  "use strict";

  const SCAN_SCOPE_SCHEMA_VERSION = 1;
  const SCOPES = Object.freeze({
    core_doudian: Object.freeze(["overview", "orders", "products", "shelf"]),
    full_doudian: Object.freeze([
      "overview", "orders", "products", "inventory", "refunds", "reviews", "shelf",
      "live", "short_video", "image_text", "search", "recommend_card", "funds",
    ]),
    ads_optional: Object.freeze([
      "qianchuan_video_library", "qianchuan_overview", "qianchuan_campaigns",
      "qianchuan_live", "qianchuan_live_dashboard",
    ]),
  });
  const ALL_PAGE_IDS = Object.freeze([...SCOPES.full_doudian, ...SCOPES.ads_optional]);
  const KNOWN_PAGE_IDS = new Set(ALL_PAGE_IDS);
  const ADS_PAGE_IDS = new Set(SCOPES.ads_optional);

  function uniqueKnownPageIds(value = []) {
    const values = Array.isArray(value) ? value : [];
    return [...new Set(values.map((item) => String(item || "").trim()).filter((item) => KNOWN_PAGE_IDS.has(item)))];
  }

  function pageIds(scopeName = "") {
    return [...(SCOPES[String(scopeName || "")] || [])];
  }

  function fullScanPageIds(options = {}) {
    return options.includeAds
      ? [...SCOPES.full_doudian, ...SCOPES.ads_optional]
      : [...SCOPES.full_doudian];
  }

  function resolveLaunchPageIds(options = {}) {
    const scope = String(options.scope || "");
    const reason = String(options.reason || "manual");
    if (scope === "full" && reason === "manual") {
      return fullScanPageIds({ includeAds: Boolean(options.accountKey) });
    }
    return uniqueKnownPageIds(options.requestedPageIds);
  }

  function receiptPlan(scan = {}) {
    const reported = uniqueKnownPageIds(
      Array.isArray(scan.planned_page_ids) && scan.planned_page_ids.length
        ? scan.planned_page_ids
        : scan.targeted_page_ids,
    );
    const scope = String(scan.scope || "");
    if (scope !== "full") {
      return {
        schema_version: SCAN_SCOPE_SCHEMA_VERSION,
        scope,
        planned_page_ids: reported,
        expected_page_ids: [...reported],
        missing_contract_page_ids: [],
        contract_complete: true,
      };
    }
    const includeAds = Boolean(scan.account_key) || reported.some((pageId) => ADS_PAGE_IDS.has(pageId));
    const expected = fullScanPageIds({ includeAds });
    const reportedSet = new Set(reported);
    const missing = expected.filter((pageId) => !reportedSet.has(pageId));
    return {
      schema_version: SCAN_SCOPE_SCHEMA_VERSION,
      scope,
      planned_page_ids: reported,
      expected_page_ids: expected,
      missing_contract_page_ids: missing,
      contract_complete: missing.length === 0,
    };
  }

  function validatePageRegistry(pageDefinitions = []) {
    const ids = (Array.isArray(pageDefinitions) ? pageDefinitions : [])
      .map((item) => String(item?.id || ""))
      .filter(Boolean);
    const duplicates = ids.filter((id, index) => ids.indexOf(id) !== index);
    const idSet = new Set(ids);
    return {
      ok: duplicates.length === 0
        && ALL_PAGE_IDS.every((id) => idSet.has(id))
        && ids.every((id) => KNOWN_PAGE_IDS.has(id)),
      duplicate_page_ids: [...new Set(duplicates)],
      missing_page_ids: ALL_PAGE_IDS.filter((id) => !idSet.has(id)),
      unknown_page_ids: ids.filter((id) => !KNOWN_PAGE_IDS.has(id)),
    };
  }

  globalThis.DianAgentScanScopePolicy = Object.freeze({
    schemaVersion: SCAN_SCOPE_SCHEMA_VERSION,
    scopes: SCOPES,
    allPageIds: ALL_PAGE_IDS,
    pageIds,
    uniqueKnownPageIds,
    fullScanPageIds,
    resolveLaunchPageIds,
    receiptPlan,
    validatePageRegistry,
  });
})();
