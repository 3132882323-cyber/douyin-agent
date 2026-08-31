const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "sidepanel.html"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "sidepanel.css"), "utf8");
const script = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");

assert.match(html, /id="store-linked-account"[\s\S]*?id="linked-account-select"[\s\S]*?id="select-linked-account-button"[\s\S]*?id="unlink-account-button"/);
assert.match(script, /linkedKeys\.length > 1[\s\S]*?一次巡检只允许选择一个/);
assert.match(script, /id="select-linked-account-button"|getElementById\("select-linked-account-button"\)/);
assert.match(script, /bridgeFetch\("\/stores\/select"[\s\S]*?account_key: accountKey/);
assert.match(script, /bridgeFetch\("\/stores\/unlink"[\s\S]*?account_key: accountKey/);
assert.match(script, /历史快照不会删除/);
assert.match(css, /\.store-linked-account[\s\S]*?#store-linked-account-result/);

const scopedStart = script.indexOf("function scopedSelectedAccountKey");
const scopedEnd = script.indexOf("\nfunction renderQianchuanAccounts", scopedStart);
assert.ok(scopedStart >= 0 && scopedEnd > scopedStart, "scoped account selector should exist");
const scopeSandbox = {};
vm.runInNewContext(script.slice(scopedStart, scopedEnd), scopeSandbox);
assert.equal(
  scopeSandbox.scopedSelectedAccountKey({ account_keys: ["account-a"] }, "ACCOUNT-A"),
  "account-a",
);
assert.equal(
  scopeSandbox.scopedSelectedAccountKey({ account_keys: ["account-b"] }, "account-a"),
  "",
  "an account from the previous store must never survive a store switch",
);
const reconciled = scopeSandbox.reconcileQianchuanScope({
  stores: [
    { key: "store-a", account_keys: ["account-a"] },
    { key: "store-b", account_keys: ["account-b"] },
  ],
  selected_store_key: "store-b",
  selected_account_key: "account-a",
});
assert.equal(reconciled.storeKey, "store-b");
assert.equal(reconciled.accountKey, "", "a selected account not linked to the selected store must fail closed");
const unavailable = scopeSandbox.reconcileQianchuanScope({
  stores: [{ key: "store-a", account_keys: ["account-a"] }],
  selected_store_key: "store-a",
  selected_account_key: "account-a",
}, { available: false });
assert.equal(unavailable.storeKey, "");
assert.equal(unavailable.accountKey, "");
assert.equal(unavailable.available, false);
assert.match(
  script,
  /selectedStoreKey = requestedStoreKey;\s*selectedQianchuanAccount = "";[\s\S]{0,800}?bridgeFetch\("\/stores\/select"/,
);

console.log("store and qianchuan account mapping UI tests passed");
