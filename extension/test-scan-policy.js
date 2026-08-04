const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(require.resolve("./scan-policy.js"), "utf8");
const context = { globalThis: null };
context.globalThis = context;
vm.runInNewContext(source, context, { filename: "scan-policy.js" });

const policy = context.DianAgentScanPolicy;
assert.ok(policy);

assert.strictEqual(
  policy.matchAccount(
    { key: "acct_platform_b", label: "同名旗舰店", identity_source: "platform_id" },
    { key: "acct_platform_a", label: "同名旗舰店", identity_source: "platform_id" },
  ).code,
  "ACCOUNT_MISMATCH",
);

assert.strictEqual(
  policy.matchAccount(
    { key: "acct_platform_a", label: "同名旗舰店", identity_source: "platform_id" },
    { key: "acct_label", label: "同名旗舰店", identity_source: "account_label" },
  ).code,
  "ACCOUNT_MISMATCH",
);

const storeMismatch = new Error("当前页面所属店铺与已选店铺不一致，已停止巡检且不会混合数据。");
assert.strictEqual(policy.errorCode(storeMismatch), "STORE_MISMATCH");
assert.strictEqual(policy.isNonRetryable(storeMismatch), true);

const unresolved = new Error("未识别当前千川账号。巡检已停止刷新，请确认账号后重试。");
assert.strictEqual(policy.isNonRetryable(unresolved), true);
assert.strictEqual(policy.errorCode(unresolved), "ACCOUNT_UNRESOLVED");
assert.strictEqual(policy.isNonRetryable(new Error("页面加载超时")), false);

const ranked = policy.rankSeedTabs([
  { id: 1, active: true, lastAccessed: 300 },
  { id: 2, active: false, lastAccessed: 100 },
  { id: 3, active: false, lastAccessed: 500 },
], 2);
assert.deepStrictEqual(Array.from(ranked, (tab) => tab.id), [2, 1, 3]);

assert.strictEqual(policy.isQianchuanUrl("https://qianchuan.jinritemai.com/dataV2/roi2-material-analysis"), true);
assert.strictEqual(policy.isQianchuanUrl("chrome-extension://workbench/sidepanel.html"), false);

const qianchuanTabs = [
  { id: 11, url: "https://qianchuan.jinritemai.com/home", active: false, lastAccessed: 100 },
  { id: 12, url: "https://qianchuan.jinritemai.com/dataV2/roi2-material-analysis", active: false, lastAccessed: 300 },
];
const recentSelection = policy.selectQianchuanSyncTab(
  qianchuanTabs,
  { id: 99, url: "chrome-extension://workbench/sidepanel.html", active: true },
  11,
  12,
);
assert.strictEqual(recentSelection.tab.id, 11);
assert.strictEqual(recentSelection.matchedBy, "recent");

const activeSelection = policy.selectQianchuanSyncTab(
  qianchuanTabs,
  { id: 12, url: qianchuanTabs[1].url, active: true },
  11,
  null,
);
assert.strictEqual(activeSelection.tab.id, 12);
assert.strictEqual(activeSelection.matchedBy, "active");

const seedSelection = policy.selectQianchuanSyncTab(
  qianchuanTabs,
  { id: 99, url: "chrome-extension://workbench/sidepanel.html", active: true },
  404,
  12,
);
assert.strictEqual(seedSelection.tab.id, 12);
assert.strictEqual(seedSelection.matchedBy, "seed");

const missingSelection = policy.selectQianchuanSyncTab([], null, 11, 12);
assert.strictEqual(missingSelection.tab, null);
assert.strictEqual(missingSelection.matchedBy, "none");

console.log("scan-policy tests passed");
