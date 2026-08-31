const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

const policySource = fs.readFileSync(require.resolve("./scan-scope-policy.js"), "utf8");
const context = { globalThis: null };
context.globalThis = context;
vm.runInNewContext(policySource, context, { filename: "scan-scope-policy.js" });

const policy = context.DianAgentScanScopePolicy;
assert.ok(policy);

const core = Array.from(policy.scopes.core_doudian);
const fullDoudian = Array.from(policy.scopes.full_doudian);
const adsOptional = Array.from(policy.scopes.ads_optional);
assert.deepStrictEqual(core, ["overview", "orders", "products", "shelf"]);
assert.strictEqual(fullDoudian.length, 13);
assert.ok(fullDoudian.includes("search"));
assert.ok(fullDoudian.includes("funds"));
assert.strictEqual(adsOptional.length, 5);

const withoutAds = Array.from(policy.fullScanPageIds({ includeAds: false }));
const withAds = Array.from(policy.fullScanPageIds({ includeAds: true }));
assert.deepStrictEqual(withoutAds, fullDoudian, "无千川账户时仍必须覆盖全部抖店页面");
assert.deepStrictEqual(withAds.slice(0, fullDoudian.length), fullDoudian, "千川只追加页面，不得改变抖店分母");
assert.deepStrictEqual(withAds.slice(fullDoudian.length), adsOptional);

assert.deepStrictEqual(
  Array.from(policy.resolveLaunchPageIds({
    scope: "full",
    reason: "manual",
    requestedPageIds: ["overview", "orders"],
    accountKey: "",
  })),
  fullDoudian,
  "手工全店巡检不能被调用方缩小为伪全量",
);
assert.deepStrictEqual(
  Array.from(policy.resolveLaunchPageIds({
    scope: "quick",
    reason: "manual",
    requestedPageIds: ["overview", "orders"],
  })),
  ["overview", "orders"],
  "快速巡店继续使用显式目标页",
);

const legacyReduced = policy.receiptPlan({
  scope: "full",
  account_key: "",
  planned_page_ids: fullDoudian.filter((id) => !["search", "funds"].includes(id)),
});
assert.strictEqual(legacyReduced.contract_complete, false);
assert.deepStrictEqual(Array.from(legacyReduced.missing_contract_page_ids), ["search", "funds"]);
assert.deepStrictEqual(Array.from(legacyReduced.expected_page_ids), fullDoudian);

const accountPlan = policy.receiptPlan({
  scope: "full",
  account_key: "acct_a",
  planned_page_ids: withAds,
});
assert.strictEqual(accountPlan.contract_complete, true);
assert.deepStrictEqual(Array.from(accountPlan.expected_page_ids), withAds);

const background = fs.readFileSync(require.resolve("./background.js"), "utf8");
const pageRegistryBlock = background.match(/const FULL_SCAN_PAGES = \[([\s\S]*?)\n\];/);
assert.ok(pageRegistryBlock, "background page registry must remain discoverable");
const registryIds = [...pageRegistryBlock[1].matchAll(/\{\s*id:\s*"([a-z0-9_-]+)"/g)].map((match) => match[1]);
assert.strictEqual(policy.validatePageRegistry(registryIds.map((id) => ({ id }))).ok, true);
assert.match(background, /importScripts\([^)]*"scan-scope-policy\.js"/);
assert.match(background, /resolveLaunchPageIds\(/);
assert.match(background, /fullScanPageIds\(\{ includeAds: Boolean\(accountKey\) \}\)/);

const sidepanel = fs.readFileSync(require.resolve("./sidepanel.js"), "utf8");
const sidepanelHtml = fs.readFileSync(require.resolve("./sidepanel.html"), "utf8");
assert.doesNotMatch(sidepanel, /DOUDIAN_SCAN_PAGE_IDS/);
assert.match(sidepanel, /fullScanPageIds\(\{ includeAds: Boolean\(selectedQianchuanAccount\) \}\)/);
assert.match(sidepanel, /page_ids: plannedPageIds/);
assert.ok(sidepanelHtml.indexOf('src="scan-scope-policy.js"') < sidepanelHtml.indexOf('src="sidepanel.js"'));

const receiver = fs.readFileSync(require.resolve("../bridge/http_receiver.py"), "utf8");
function pythonTuple(name) {
  const match = receiver.match(new RegExp(`${name} = \\(([\\s\\S]*?)\\)`));
  assert.ok(match, `${name} must exist in the receipt contract`);
  return [...match[1].matchAll(/"([a-z0-9_-]+)"/g)].map((item) => item[1]);
}
assert.deepStrictEqual(pythonTuple("SCAN_CORE_DOUDIAN_PAGE_IDS"), core);
assert.deepStrictEqual(pythonTuple("SCAN_FULL_DOUDIAN_PAGE_IDS"), fullDoudian);
assert.deepStrictEqual(pythonTuple("SCAN_ADS_OPTIONAL_PAGE_IDS"), adsOptional);

console.log("scan scope policy tests passed");
