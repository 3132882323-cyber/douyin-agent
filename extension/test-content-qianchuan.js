const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(require.resolve("./content-qianchuan.js"), "utf8");

async function collectAccount({ href, pathname, search = "", bodyText = "", selectorValues = {} }) {
  let listener;
  const context = {
    URLSearchParams,
    location: {
      href,
      pathname,
      search,
      hostname: "qianchuan.jinritemai.com",
    },
    document: {
      title: "巨量千川",
      body: { innerText: bodyText },
      documentElement: {},
      querySelectorAll(selector) {
        return (selectorValues[selector] || []).map((innerText) => ({
          innerText,
          getClientRects: () => [1],
        }));
      },
    },
    chrome: {
      storage: { local: { get: async () => ({ settings: { privacyMode: true } }) } },
      runtime: {
        onMessage: { addListener(callback) { listener = callback; } },
        async sendMessage(message) {
          return { ok: true, account: message.data.account };
        },
      },
    },
    DianAgentExtractor: {
      async collect(sourceName, pageType) {
        return { source: sourceName, page_type: pageType, quality: { score: 80 } };
      },
    },
    MutationObserver: class {
      observe() {}
    },
    setTimeout() { return 1; },
    clearTimeout() {},
    console,
  };
  context.globalThis = context;
  vm.runInNewContext(source, context, { filename: "content-qianchuan.js" });
  assert.strictEqual(typeof listener, "function");
  return new Promise((resolve, reject) => {
    listener({ type: "collect-now", reason: "test" }, {}, (response) => {
      if (!response?.ok) reject(new Error(response?.error || "capture failed"));
      else resolve(response.account);
    });
  });
}

(async () => {
  const real = await collectAccount({
    href: "https://qianchuan.jinritemai.com/home",
    pathname: "/home",
    bodyText: "我的资金 账户明细 账户余额 0.00 元 立即充值",
    selectorValues: {
      "[class*='shopName']": ["兽醒纪男士活力裤"],
      "[class*='account'] [class*='name']": ["我的资金 账户明细 账户余额 0.00 元 立即充值"],
    },
  });
  assert.strictEqual(real.label, "兽醒纪男士活力裤");
  assert.strictEqual(real.identity_source, "account_label");

  const falseAccount = await collectAccount({
    href: "https://qianchuan.jinritemai.com/home",
    pathname: "/home",
    bodyText: "当前账号\n店铺\n我的资金 账户余额 0.00 元",
    selectorValues: {
      "[class*='account'] [class*='name']": ["店铺"],
    },
  });
  assert.strictEqual(falseAccount, null);

  const idOnly = await collectAccount({
    href: "https://qianchuan.jinritemai.com/home?advertiser_id=12345678",
    pathname: "/home",
    search: "?advertiser_id=12345678",
  });
  assert.strictEqual(idOnly.label, "千川账号 · 5678");
  assert.strictEqual(idOnly.identity_source, "platform_id");

  console.log("content-qianchuan tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
