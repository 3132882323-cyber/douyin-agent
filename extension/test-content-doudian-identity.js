const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(require.resolve("./content-doudian.js"), "utf8");

async function capture({ pathname, search = "", attrs = [], storageValues = {}, bodyText = "", bootstrapScripts = [] }) {
  let listener;
  let pushed;
  const context = {
    URLSearchParams,
    location: { href: `https://fxg.jinritemai.com${pathname}${search}`, pathname, search, hash: "" },
    document: {
      documentElement: {},
      body: { innerText: bodyText },
      scripts: bootstrapScripts.map((value) => typeof value === "string"
        ? { src: "", textContent: value }
        : { src: value.src || "", textContent: value.textContent || "" }),
      querySelector() { return null; },
      querySelectorAll(selector) {
        return selector === "[data-shop-id], [data-store-id]" ? attrs.map((value) => ({
          getAttribute(name) { return name === "data-shop-id" ? value : null; },
        })) : [];
      },
    },
    chrome: {
      storage: { local: { get: async () => ({ settings: { privacyMode: true } }) } },
      runtime: {
        onMessage: { addListener(callback) { listener = callback; } },
        async sendMessage(message) {
          pushed = message.data;
          const claim = pushed.identity_claims?.[0];
          return { ok: true, store: claim ? { key: `resolved_${claim.raw_id}` } : null };
        },
      },
    },
    sessionStorage: {
      get length() { return Object.keys(storageValues).length; },
      key(index) { return Object.keys(storageValues)[index] || null; },
      getItem(key) { return storageValues[key] ?? null; },
    },
    localStorage: { length: 0, key() { return null; }, getItem() { return null; } },
    DianAgentExtractor: { async collect(sourceName, pageType) { return { source: sourceName, page_type: pageType, quality: { score: 80 } }; } },
    MutationObserver: class { observe() {} },
    setTimeout() { return 1; },
    clearTimeout() {},
  };
  context.globalThis = context;
  vm.runInNewContext(source, context, { filename: "content-doudian.js" });
  const response = await new Promise((resolve, reject) => {
    listener({ type: "collect-now", reason: "test" }, {}, (result) => result?.ok ? resolve(result) : reject(new Error(result?.error || "capture failed")));
  });
  return { response, pushed };
}

(async () => {
  const overview = await capture({ pathname: "/ffa/mshop/homepage/index", search: "?shop_id=778899" });
  const orders = await capture({ pathname: "/ffa/morder/order/list", search: "?shop_id=778899" });
  assert.strictEqual(overview.response.store.key, orders.response.store.key);
  assert.strictEqual(overview.pushed.identity_claims[0].kind, "douyin_shop_id");

  const unresolved = await capture({ pathname: "/ffa/mshop/homepage/index" });
  assert.strictEqual(unresolved.response.store, null);
  assert.strictEqual(unresolved.pushed.identity_status, "unresolved");

  const visibleOverviewIdentity = await capture({
    pathname: "/ffa/mshop/homepage/index",
    bodyText: "欢迎回来  店铺ID：55667788  今日经营数据",
  });
  assert.strictEqual(visibleOverviewIdentity.response.store.key, "resolved_55667788");
  assert.strictEqual(visibleOverviewIdentity.pushed.identity_claims[0].evidence_source, "overview_visible_text");

  const innerPageVisibleTextIsNotProof = await capture({
    pathname: "/ffa/morder/order/list",
    bodyText: "订单所属店铺ID：55667788",
  });
  assert.strictEqual(innerPageVisibleTextIsNotProof.pushed.identity_status, "unresolved");

  const bootstrapIdentity = await capture({
    pathname: "/ffa/mshop/homepage/index",
    bootstrapScripts: ['window.__BOOTSTRAP__={"shop":{"sec_shop_id":"55667788","shop_type":1}}'],
  });
  assert.strictEqual(bootstrapIdentity.response.store.key, "resolved_55667788");
  assert.strictEqual(bootstrapIdentity.pushed.identity_claims[0].evidence_source, "bootstrap_sec_shop_id");

  const externalScriptIsNotIdentity = await capture({
    pathname: "/ffa/mshop/homepage/index",
    bootstrapScripts: [{ src: "https://example.invalid/app.js", textContent: '{"sec_shop_id":"55667788"}' }],
  });
  assert.strictEqual(externalScriptIsNotIdentity.pushed.identity_status, "unresolved");

  const ambiguousBootstrapIsNotProof = await capture({
    pathname: "/ffa/mshop/homepage/index",
    bootstrapScripts: ['{"sec_shop_id":"55667788"}', '{"sec_shop_id":"11223344"}'],
  });
  assert.strictEqual(ambiguousBootstrapIsNotProof.pushed.identity_status, "unresolved");

  const bootstrapConflict = await capture({
    pathname: "/ffa/mshop/homepage/index",
    search: "?shop_id=778899",
    bootstrapScripts: ['{"sec_shop_id":"55667788"}'],
  });
  assert.strictEqual(bootstrapConflict.pushed.identity_status, "conflict");

  const conflict = await capture({ pathname: "/ffa/mshop/homepage/index", search: "?shop_id=778899", attrs: ["112233"] });
  assert.strictEqual(conflict.response.store, null);
  assert.strictEqual(conflict.pushed.identity_status, "conflict");
  assert.deepStrictEqual(Array.from(conflict.pushed.identity_claims), []);
  assert.deepStrictEqual(Array.from(conflict.pushed.identity_conflicts), ["douyin_shop_id"]);

  const ambiguousCache = await capture({
    pathname: "/ffa/mshop/homepage/index",
    storageValues: { shopId: "778899", store_id: "112233" },
  });
  assert.strictEqual(ambiguousCache.response.store, null);
  assert.strictEqual(ambiguousCache.pushed.identity_status, "unresolved");
  assert.deepStrictEqual(Array.from(ambiguousCache.pushed.identity_claims), []);

  const historicalShopListIsNotProof = await capture({
    pathname: "/ffa/mshop/homepage/index",
    storageValues: { shopList: JSON.stringify([{ shop_id: "778899" }]) },
  });
  assert.strictEqual(historicalShopListIsNotProof.pushed.identity_status, "unresolved");
  assert.deepStrictEqual(Array.from(historicalShopListIsNotProof.pushed.identity_claims), []);

  const genericScalarCacheIsNotProof = await capture({
    pathname: "/ffa/mshop/homepage/index",
    storageValues: { shopId: "778899" },
  });
  assert.strictEqual(genericScalarCacheIsNotProof.pushed.identity_status, "unresolved");

  const activeScalarCache = await capture({
    pathname: "/ffa/mshop/homepage/index",
    storageValues: { activeShopId: "778899" },
  });
  assert.strictEqual(activeScalarCache.pushed.identity_claims[0].raw_id, "778899");
  assert.strictEqual(activeScalarCache.pushed.identity_claims[0].evidence_source, "allowlisted_storage");

  console.log("content-doudian identity tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
