const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const policy = require("./execution-sender-policy.js");

const extensionId = "abcdefghijklmnopabcdefghijklmnop";

assert.equal(policy.isTrustedExecutionSender({
  id: extensionId,
  url: `chrome-extension://${extensionId}/sidepanel.html`,
}, extensionId), true);

assert.equal(policy.isTrustedExecutionSender({
  id: extensionId,
  url: "https://qianchuan.jinritemai.com/ad/plan",
  tab: { id: 7 },
}, extensionId), false);

assert.equal(policy.isTrustedExecutionSender({
  id: extensionId,
  url: `chrome-extension://${extensionId}/popup.html`,
}, extensionId), false);

assert.equal(policy.isTrustedExecutionSender({
  id: "ponmlkjihgfedcbaponmlkjihgfedcba",
  url: `chrome-extension://${extensionId}/sidepanel.html`,
}, extensionId), false);

assert.equal(policy.isTrustedExtensionPageSender({
  id: extensionId,
  url: `chrome-extension://${extensionId}/popup.html?from=toolbar`,
}, extensionId), true);
assert.equal(policy.isTrustedExtensionPageSender({
  id: extensionId,
  url: `chrome-extension://${extensionId}/unknown.html`,
}, extensionId), false);

assert.equal(policy.isTrustedPageDataSender({
  id: extensionId,
  url: "https://fxg.jinritemai.com/ffa/mshop/homepage/index",
  frameId: 0,
  tab: { id: 7 },
}, extensionId, "doudian"), true);
assert.equal(policy.isTrustedPageDataSender({
  id: extensionId,
  url: "https://fxg.jinritemai.com/ffa/mshop/homepage/index",
  frameId: 0,
  tab: { id: 7 },
}, extensionId, "qianchuan"), false, "a Doudian page cannot claim to be Qianchuan");
assert.equal(policy.isTrustedPageDataSender({
  id: extensionId,
  url: "https://qianchuan.jinritemai.com/uni-prom",
  frameId: 3,
  tab: { id: 9 },
}, extensionId, "qianchuan"), false, "subframes cannot inject business snapshots");
assert.equal(policy.isTrustedPageDataSender({
  id: extensionId,
  url: "https://qianchuan.jinritemai.com.evil.example/uni-prom",
  frameId: 0,
  tab: { id: 9 },
}, extensionId, "qianchuan"), false, "lookalike hostnames must fail closed");
assert.doesNotThrow(() => policy.isTrustedPageDataSender({
  id: extensionId,
  url: "https://qianchuan.jinritemai.com/uni-prom",
  frameId: 0,
  tab: { id: 9 },
}, extensionId, "toString"));
assert.equal(policy.isTrustedPageDataSender({
  id: extensionId,
  url: "https://qianchuan.jinritemai.com/uni-prom",
  frameId: 0,
  tab: { id: 9 },
}, extensionId, "__proto__"), false, "prototype keys are not valid declared sources");

const background = fs.readFileSync(path.join(__dirname, "background.js"), "utf8");
assert.match(background, /isTrustedPageDataSender\(sender, chrome\.runtime\.id, message\?\.source\)/);
assert.match(background, /isTrustedExtensionPageSender\(sender, chrome\.runtime\.id\)/);
assert.match(background, /UNTRUSTED_PLATFORM_PAGE_SENDER/);
assert.match(background, /TRUSTED_PLATFORM_MESSAGE_TYPES/);
assert.match(background, /UNTRUSTED_UI_MESSAGE_SENDER/);

console.log("execution sender policy tests passed");
