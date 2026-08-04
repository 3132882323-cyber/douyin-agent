const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(require.resolve("./content-qianchuan.js"), "utf8");

async function collectIdentity({ href, pathname, search = "", hash = "", bodyText = "", selectorValues = {}, storageValues = {} }) {
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
      async collect(sourceName, pageType) { return { source: sourceName, page_type: pageType, quality: { score: 80 } }; },
      pseudonymizePlanIdentifier(value) { return value; },
    },
    MutationObserver: class { observe() {} },
    setTimeout() { return 1; },
    clearTimeout() {},
    console,
  };
  context.globalThis = context;
  vm.runInNewContext(source, context, { filename: "content-qianchuan.js" });
  assert.strictEqual(typeof listener, "function");
  const response = await new Promise((resolve, reject) => {
    listener({ type: "collect-now", reason: "test" }, {}, (result) => result?.ok ? resolve(result) : reject(new Error(result?.error || "capture failed")));
  });
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

  console.log("content-qianchuan tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
