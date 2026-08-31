const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(require.resolve("./content-qianchuan.js"), "utf8");
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

async function collectIdentity({ href, pathname, search = "", hash = "", bodyText = "", selectorValues = {}, storageValues = {}, schemaEvidence = null, duringCollect = null, reason = "test" }) {
  let listener;
  let pushedData;
  const context = {
    URLSearchParams,
    location: { href, pathname, search, hash, hostname: "qianchuan.jinritemai.com" },
    document: {
      title: "巨量千川",
      body: { innerText: bodyText },
      documentElement: {},
      querySelectorAll(selector) {
        return (selectorValues[selector] || []).map((value) => ({
          innerText: typeof value === "string" ? value : value.text || "",
          getClientRects: () => [1],
          getAttribute: (name) => typeof value === "object" ? value.attrs?.[name] || null : null,
        }));
      },
    },
    chrome: {
      storage: { local: { get: async () => ({ settings: { privacyMode: true } }) } },
      runtime: {
        onMessage: { addListener(callback) { listener = callback; } },
        async sendMessage(message) {
          pushedData = message.data;
          const claims = message.data.identity_claims || [];
          const storeClaim = claims.find((item) => item.kind === "douyin_shop_id");
          const accountClaim = claims.find((item) => item.kind === "qianchuan_advertiser_id" || item.kind === "qianchuan_account_id");
          return {
            ok: true,
            store: storeClaim ? { key: `store_${storeClaim.raw_id}`, identity_source: "hmac_douyin_shop_id" } : null,
            account: accountClaim ? { key: `acct_${accountClaim.raw_id}`, identity_source: `hmac_${accountClaim.kind}` } : null,
          };
        },
      },
    },
    sessionStorage: {
      get length() { return Object.keys(storageValues).length; },
      key(index) { return Object.keys(storageValues)[index] || null; },
      getItem(key) { return storageValues[key] ?? null; },
    },
    localStorage: { length: 0, key() { return null; }, getItem() { return null; } },
    DianAgentExtractor: {
      async collect(sourceName, pageType, _privacyMode, _reason, options = {}) {
        if (typeof duringCollect === "function") await duringCollect(context, options);
        return { source: sourceName, page_type: pageType, quality: { score: 80 } };
      },
      pseudonymizePlanIdentifier(value) { return value; },
      tableSchemaEvidence() { return schemaEvidence; },
    },
    MutationObserver: class { observe() {} },
    setTimeout() { return 1; },
    clearTimeout() {},
    console,
  };
  context.globalThis = context;
  vm.runInNewContext(source, context, { filename: "content-qianchuan.js" });
  assert.strictEqual(typeof listener, "function");
  let response;
  try {
    response = await new Promise((resolve, reject) => {
      listener({ type: "collect-now", reason }, {}, (result) => result?.ok ? resolve(result) : reject(new Error(result?.error || "capture failed")));
    });
  } catch (error) {
    error.pushedData = pushedData;
    throw error;
  }
  return { ...response, pushedData };
}

(async () => {
  const sameStoreA = await collectIdentity({ href: "https://qianchuan.jinritemai.com/home?shop_id=778899", pathname: "/home", search: "?shop_id=778899" });
  const sameStoreB = await collectIdentity({ href: "https://qianchuan.jinritemai.com/report?shop_id=778899", pathname: "/report", search: "?shop_id=778899" });
  assert.strictEqual(sameStoreA.store.key, sameStoreB.store.key);

  const combined = await collectIdentity({ href: "https://qianchuan.jinritemai.com/home?shop_id=778899&advertiser_id=12345678", pathname: "/home", search: "?shop_id=778899&advertiser_id=12345678" });
  assert.strictEqual(combined.store.key, "store_778899");
  assert.strictEqual(combined.account.key, "acct_12345678");
  assert.notStrictEqual(combined.store.key, combined.account.key);

  const chengfangAccount = await collectIdentity({
    href: "https://qianchuan.jinritemai.com/uni-prom/overall?aavid=1867768299862148",
    pathname: "/uni-prom/overall",
    search: "?aavid=1867768299862148",
    bodyText: "抖音号 投放状态 投放设置 营销服务 调控工具 乘方投放",
  });
  assert.strictEqual(chengfangAccount.account.key, "acct_1867768299862148");
  assert.strictEqual(chengfangAccount.pushedData.identity_claims[0].kind, "qianchuan_advertiser_id");
  assert.strictEqual(chengfangAccount.pushedData.identity_claims[0].confidence, "high");
  assert.strictEqual(chengfangAccount.page_type, "qianchuan_live");

  const labelOnly = await collectIdentity({
    href: "https://qianchuan.jinritemai.com/home",
    pathname: "/home",
    selectorValues: { "[class*='shopName']": ["同名旗舰店"] },
  });
  assert.strictEqual(labelOnly.store, null);
  assert.strictEqual(labelOnly.account, null);

  const advertiserA = await collectIdentity({ href: "https://qianchuan.jinritemai.com/home?advertiser_id=10000001", pathname: "/home", search: "?advertiser_id=10000001" });
  const advertiserB = await collectIdentity({ href: "https://qianchuan.jinritemai.com/home?advertiser_id=10000002", pathname: "/home", search: "?advertiser_id=10000002" });
  assert.notStrictEqual(advertiserA.account.key, advertiserB.account.key);

  const stored = await collectIdentity({
    href: "https://qianchuan.jinritemai.com/dataV2/roi2-material-analysis",
    pathname: "/dataV2/roi2-material-analysis",
    storageValues: { selected_advertiser_id: "99887766" },
  });
  assert.strictEqual(stored.account.key, "acct_99887766");
  assert.strictEqual(stored.pushedData.identity_claims[0].confidence, "medium");

  const login = await collectIdentity({ href: "https://qianchuan.jinritemai.com/login", pathname: "/login", storageValues: { selected_advertiser_id: "99887766" } });
  assert.strictEqual(login.account, null);
  assert.strictEqual(login.store, null);

  const identityConflict = await collectIdentity({
    href: "https://qianchuan.jinritemai.com/home?aavid=10000001&advertiser_id=10000002",
    pathname: "/home",
    search: "?aavid=10000001&advertiser_id=10000002",
  });
  assert.strictEqual(identityConflict.account, null);
  assert.strictEqual(identityConflict.pushedData.identity_status, "conflict");
  assert.deepStrictEqual(Array.from(identityConflict.pushedData.identity_conflicts), ["qianchuan_advertiser_id"]);

  const staleMaterialTable = await collectIdentity({
    href: "https://qianchuan.jinritemai.com/uni-prom",
    pathname: "/uni-prom",
    bodyText: "视频 操作 审核状态 素材建议 视频追投 视频类型 创建时间 综合营销ROI",
  });
  assert.strictEqual(staleMaterialTable.page_type, "materials", "旧素材表格不得按新地址误存为计划页");

  const livePlanTable = await collectIdentity({
    href: "https://qianchuan.jinritemai.com/uni-prom",
    pathname: "/uni-prom",
    bodyText: "抖音号 投放状态 投放设置 营销服务 调控工具 操作 综合营销ROI",
  });
  assert.strictEqual(livePlanTable.page_type, "qianchuan_live");

  const activeLiveOverSidebar = await collectIdentity({
    href: "https://qianchuan.jinritemai.com/uni-prom",
    pathname: "/uni-prom",
    bodyText: "商品推广 直播推广 素材管理 视频追投 视频类型 素材建议 全域推广 标准推广 乘方",
    selectorValues: { [ACTIVE_NAV_SELECTOR]: ["直播推广"] },
  });
  assert.strictEqual(activeLiveOverSidebar.page_type, "qianchuan_live", "active tab must outrank unrelated whole-page navigation text");

  const breadcrumbMaterialOverSidebar = await collectIdentity({
    href: "https://qianchuan.jinritemai.com/uni-prom",
    pathname: "/uni-prom",
    bodyText: "抖音号 投放状态 营销服务 调控工具 商品推广 直播推广 素材管理",
    selectorValues: { [BREADCRUMB_SELECTOR]: ["千川 > 素材管理 > 视频库"] },
  });
  assert.strictEqual(breadcrumbMaterialOverSidebar.page_type, "materials", "breadcrumb must outrank whole-page navigation text");

  const campaignSchemaOverActiveRoute = await collectIdentity({
    href: "https://qianchuan.jinritemai.com/uni-prom",
    pathname: "/uni-prom",
    bodyText: "素材管理 视频追投 视频类型 素材建议",
    selectorValues: { [ACTIVE_NAV_SELECTOR]: ["直播推广"] },
    schemaEvidence: { page_type: "campaigns", confidence: "high", source: "table_schema", headers: ["商品计划名称", "投放状态", "支付ROI"] },
  });
  assert.strictEqual(campaignSchemaOverActiveRoute.page_type, "campaigns", "a high-confidence rendered table schema must outrank a tab during SPA transition");

  const conflictingSchemas = await collectIdentity({
    href: "https://qianchuan.jinritemai.com/uni-prom",
    pathname: "/uni-prom",
    selectorValues: { [ACTIVE_NAV_SELECTOR]: ["商品推广"] },
    schemaEvidence: { page_type: "unknown", confidence: "conflict", source: "conflicting_table_schemas", headers: [] },
  });
  assert.strictEqual(conflictingSchemas.page_type, "unknown", "incompatible visible schemas must fail closed");

  const scopedFullDomain = await collectIdentity({
    href: "https://qianchuan.jinritemai.com/uni-prom",
    pathname: "/uni-prom",
    bodyText: "侧栏：标准推广 全域推广 乘方",
    selectorValues: { [ACTIVE_NAV_SELECTOR]: ["全域推广"] },
  });
  assert.strictEqual(scopedFullDomain.pushedData.promotion_context.promotion_mode, "full_domain");
  assert.strictEqual(scopedFullDomain.pushedData.promotion_context.evidence.source, "active_navigation");

  const conflictingActiveModes = await collectIdentity({
    href: "https://qianchuan.jinritemai.com/uni-prom",
    pathname: "/uni-prom",
    bodyText: "标准推广 全域推广",
    selectorValues: { [ACTIVE_NAV_SELECTOR]: ["标准推广", "全域推广"] },
  });
  assert.strictEqual(conflictingActiveModes.pushedData.promotion_context.promotion_mode, "unknown");
  assert.strictEqual(conflictingActiveModes.pushedData.promotion_context.evidence.source, "conflicting_scoped_labels");

  await assert.rejects(
    collectIdentity({
      href: "https://qianchuan.jinritemai.com/uni-prom?shop_id=store_a&aavid=account_a",
      pathname: "/uni-prom",
      search: "?shop_id=store_a&aavid=account_a",
      bodyText: "标准推广",
      reason: "full-scan-list-qianchuan_campaigns",
      duringCollect: async (context, options) => {
        assert.strictEqual(typeof options.assertContextStable, "function");
        options.assertContextStable();
        context.location.href = "https://qianchuan.jinritemai.com/uni-prom?shop_id=store_b&aavid=account_b";
        context.location.search = "?shop_id=store_b&aavid=account_b";
        let bError = null;
        try { options.assertContextStable(); } catch (error) { bError = error; }
        assert.strictEqual(bError?.code, "COLLECTION_CONTEXT_CHANGED");
        context.location.href = "https://qianchuan.jinritemai.com/uni-prom?shop_id=store_a&aavid=account_a";
        context.location.search = "?shop_id=store_a&aavid=account_a";
        let abaError = null;
        try { options.assertContextStable(); } catch (error) { abaError = error; }
        assert.strictEqual(abaError?.code, "COLLECTION_CONTEXT_CHANGED", "returning to A must not reset the Qianchuan context epoch");
        throw abaError;
      },
    }),
    (error) => {
      assert.match(error.message, /QIANCHUAN_CAPTURE_CONTEXT_CHANGED/);
      assert.strictEqual(error.pushedData, undefined, "mixed-account rows must never reach page-data storage");
      return true;
    },
  );

  console.log("content-qianchuan tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
