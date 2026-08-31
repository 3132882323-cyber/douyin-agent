const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const commonSource = fs.readFileSync(path.join(__dirname, "content-common.js"), "utf8");
const doudianSource = fs.readFileSync(path.join(__dirname, "content-doudian.js"), "utf8");

function visibleElement(extra = {}) {
  return { getClientRects: () => [1], ...extra };
}

function cell(text, header = false) {
  return visibleElement({
    innerText: text,
    getAttribute: () => null,
    querySelectorAll: () => [],
    header,
  });
}

function row(values, header = false) {
  const cells = values.map((value) => cell(value, header));
  return visibleElement({
    querySelectorAll(selector) {
      if (selector === "th, [role='columnheader']") return header ? cells : [];
      return cells;
    },
  });
}

function tableFor(identity) {
  return visibleElement({
    getAttribute: () => null,
    closest: () => null,
    querySelectorAll(selector) {
      if (selector === "th, [role='columnheader']") return [cell("计划", true), cell("计划ID", true)];
      return [row(["计划", "计划ID"], true), row([`店铺${identity}计划`, `plan_${identity}`])];
    },
  });
}

function commonRuntime({ phase }) {
  let identity = "A";
  let contextEpoch = 0;
  let timerCount = 0;
  let pageClicks = 0;
  const scrollContainer = visibleElement({
    clientHeight: 200,
    scrollHeight: 1_000,
    scrollTop: 0,
  });
  const nextButton = visibleElement({
    disabled: false,
    className: "",
    getAttribute: () => null,
    click() {
      pageClicks += 1;
      identity = "B";
      contextEpoch += 1;
    },
  });
  const document = {
    title: "测试计划页",
    body: { innerText: "标准推广" },
    querySelectorAll(selector) {
      if (selector.startsWith("table, [role='table']")) return [tableFor(identity)];
      if (selector.startsWith("[role='grid'], [class*='virtual']")) return phase === "scroll" ? [scrollContainer] : [];
      if (selector.startsWith("button[aria-label*='下一页']")) return phase === "pagination" && pageClicks === 0 ? [nextButton] : [];
      return [];
    },
  };
  const context = {
    document,
    location: { origin: "https://qianchuan.jinritemai.com", pathname: "/campaigns" },
    getComputedStyle: () => ({ display: "block", visibility: "visible" }),
    setTimeout(callback) {
      timerCount += 1;
      if (phase === "scroll" && timerCount === 1) {
        // The visible identity returns to A before the await resolves, but the
        // monotonic epoch proves that an A→B→A transition happened in-between.
        identity = "B";
        contextEpoch += 1;
        identity = "A";
        contextEpoch += 1;
      } else if (phase === "pagination" && timerCount === 1) {
        identity = "A";
        contextEpoch += 1;
      }
      callback();
      return timerCount;
    },
    clearTimeout() {},
    console,
  };
  context.globalThis = context;
  vm.runInNewContext(commonSource, context, { filename: `content-common-${phase}.vm.js` });
  const baselineEpoch = contextEpoch;
  const assertContextStable = () => {
    if (contextEpoch !== baselineEpoch) {
      const error = new Error("COLLECTION_CONTEXT_CHANGED: collection context epoch changed");
      error.code = "COLLECTION_CONTEXT_CHANGED";
      throw error;
    }
  };
  return {
    collect: () => context.DianAgentExtractor.collect(
      "qianchuan",
      "campaigns",
      true,
      "full-scan-list-qianchuan_campaigns",
      { assertContextStable },
    ),
  };
}

async function doudianAbaCapture() {
  let listener;
  const pushed = [];
  const location = {
    href: "https://fxg.jinritemai.com/ffa/g/list?shop_id=shop_A",
    pathname: "/ffa/g/list",
    search: "?shop_id=shop_A",
    hash: "",
  };
  const setShop = (shop) => {
    location.search = `?shop_id=${shop}`;
    location.href = `https://fxg.jinritemai.com/ffa/g/list${location.search}`;
  };
  const context = {
    URLSearchParams,
    location,
    document: {
      documentElement: {},
      body: { innerText: "商品列表" },
      querySelector: () => null,
      querySelectorAll: () => [],
    },
    chrome: {
      storage: { local: { get: async () => ({ settings: { privacyMode: true } }) } },
      runtime: {
        onMessage: { addListener(callback) { listener = callback; } },
        sendMessage: async (message) => {
          pushed.push(message);
          return { ok: true };
        },
      },
    },
    sessionStorage: { length: 0, key: () => null, getItem: () => null },
    localStorage: { length: 0, key: () => null, getItem: () => null },
    DianAgentExtractor: {
      async collect(_source, pageType, _privacy, _reason, options = {}) {
        assert.equal(typeof options.assertContextStable, "function", "full-scan capture must pass a context epoch guard into content-common");
        options.assertContextStable();
        setShop("shop_B");
        let bError = null;
        try { options.assertContextStable(); } catch (error) { bError = error; }
        assert.equal(bError?.code, "COLLECTION_CONTEXT_CHANGED");
        setShop("shop_A");
        let abaError = null;
        try { options.assertContextStable(); } catch (error) { abaError = error; }
        assert.equal(abaError?.code, "COLLECTION_CONTEXT_CHANGED", "returning to A must not reset the monotonic context epoch");
        throw abaError;
      },
    },
    MutationObserver: class { observe() {} },
    setTimeout: () => 1,
    clearTimeout() {},
    console,
  };
  context.globalThis = context;
  vm.runInNewContext(doudianSource, context, { filename: "content-doudian-aba.vm.js" });
  const result = await new Promise((resolve) => {
    listener({ type: "collect-now", reason: "full-scan-list-products" }, {}, resolve);
  });
  return { result, pushed };
}

(async () => {
  await assert.rejects(commonRuntime({ phase: "scroll" }).collect(), (error) => error.code === "COLLECTION_CONTEXT_CHANGED");
  await assert.rejects(commonRuntime({ phase: "pagination" }).collect(), (error) => error.code === "COLLECTION_CONTEXT_CHANGED");

  const capture = await doudianAbaCapture();
  assert.equal(capture.result.ok, false);
  assert.match(capture.result.error, /COLLECTION_CONTEXT_CHANGED/);
  assert.equal(capture.result.code, "COLLECTION_CONTEXT_CHANGED");
  assert.equal(capture.result.error_code, "COLLECTION_CONTEXT_CHANGED");
  assert.equal(capture.pushed.length, 0, "an A→B→A full-scan must not push a mixed snapshot");
  console.log("content-common context epoch tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
