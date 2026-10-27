"use strict";
const assert = require("node:assert/strict");
const core = require("./product-move-core.js");

// Synthetic decimal examples only. The price rule never reads or writes a shop.
let assertions = 0;
function equal(actual, expected) {
  assert.equal(actual, expected);
  assertions++;
}
function check(raw, rule, expected) {
  const result = core.calculatePriceRule(raw, rule);
  equal(result.ok, true);
  equal(result.value, expected);
}

check("100", { mode: "formula", factor: "1.2", offset: "2", decimals: "2" }, "122.00");
check("19.9900", { mode: "formula", factor: "1", offset: "0", decimals: "0" }, "19.9900");
check("0", { mode: "set", value: "0" }, "0");
check("10.00", { mode: "formula", factor: "0", offset: "25", decimals: "2" }, "25.00");
check("1.005", { mode: "formula", factor: "1", offset: "0.00001", decimals: "2" }, "1.01");

for (const raw of ["", undefined, "¥10", "NaN", "-1"]) {
  equal(core.calculatePriceRule(raw, { mode: "formula", factor: "1", offset: "0", decimals: "2" }).ok, false);
}
equal(core.calculatePriceRule("1", { mode: "formula", factor: "1", offset: "-2", decimals: "2" }).ok, false);

let getters = 0;
const rule = { mode: "formula", factor: "1", offset: "0", decimals: "2" };
Object.defineProperty(rule, "factor", { get() { getters++; return "1"; } });
equal(core.calculatePriceRule("1", rule).ok, false);
equal(getters, 0);
console.log(`product move price rule: ${assertions} assertions passed`);
