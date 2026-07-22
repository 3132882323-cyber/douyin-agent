const assert = require("node:assert/strict");
require("./content-common.js");

assert.equal(
  globalThis.DianAgentExtractor.maskText("电话 13812345678，邮箱 buyer@example.com"),
  "电话 138****5678，邮箱 bu***@example.com",
);
assert.equal(
  globalThis.DianAgentExtractor.maskText("身份证 110101199001011234"),
  "身份证 110101********1234",
);
assert.equal(globalThis.DianAgentExtractor.compact("  A  \n\n\n  B  "), "A \n\n B");
assert.equal(globalThis.DianAgentExtractor.maskText("订单号 123456789012345678"), "订单号 [已隐藏]");
assert.equal(globalThis.DianAgentExtractor.isSensitiveHeader("收货地址"), true);
assert.equal(globalThis.DianAgentExtractor.isSensitiveHeader("商品名称"), false);

console.log("content-common tests passed");
