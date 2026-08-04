const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(require.resolve("./content-doudian.js"), "utf8");

async function capture({ pathname, search = "", attrs = [], storageValues = {} }) {
  let listener;
  let pushed;
  const context = {
    URLSearchParams,
    location: { href: `https://fxg.jinritemai.com${pathname}${search}`, pathname, search, hash: "" },
    document: {
      documentElement: {},
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

  const conflict = await capture({ pathname: "/ffa/mshop/homepage/index", search: "?shop_id=778899", attrs: ["112233"] });
  assert.strictEqual(conflict.response.store, null);
  assert.strictEqual(conflict.pushed.identity_status, "conflict");
  assert.deepStrictEqual(Array.from(conflict.pushed.identity_claims), []);

  console.log("content-doudian identity tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
