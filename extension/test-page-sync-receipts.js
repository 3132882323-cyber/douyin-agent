"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const background = fs.readFileSync(path.join(__dirname, "background.js"), "utf8");
const start = background.indexOf("async function recordPageSyncReceipt");
const end = background.indexOf("\nasync function loadExecutionReadbackJobs", start);
assert.ok(start >= 0 && end > start, "page sync receipt implementation should be extractable");

const stored = {};
const context = {
  Date,
  String,
  PAGE_SYNC_RECEIPTS_KEY: "pageSyncReceiptsV1",
  async mutateLocalStorage(_keys, mutator) {
    const updates = await mutator(stored);
    Object.assign(stored, updates);
    return updates;
  },
  async syncCurrentPage(source) {
    return {
      source,
      page_type: source === "qianchuan" ? "campaigns" : "overview",
      account: source === "qianchuan" ? { key: "account-a", label: "账户 A" } : null,
      store: { key: "store-a" },
    };
  },
};
vm.runInNewContext(`${background.slice(start, end)}\nthis.receipts = { recordPageSyncReceipt, syncCurrentPageWithReceipt };`, context);

(async () => {
  await context.receipts.syncCurrentPageWithReceipt("doudian");
  await context.receipts.syncCurrentPageWithReceipt("qianchuan");
  const receipts = stored.pageSyncReceiptsV1;
  assert.equal(receipts.doudian.status, "success");
  assert.equal(receipts.doudian.page_type, "overview");
  assert.equal(receipts.qianchuan.status, "success");
  assert.equal(receipts.qianchuan.page_type, "campaigns");
  assert.equal(receipts.qianchuan.account_label, "账户 A");
  assert.ok(receipts.qianchuan.last_success_at > 0);

  const previousSuccess = receipts.qianchuan.last_success_at;
  context.syncCurrentPage = async () => {
    const error = new Error("当前页不是计划页");
    error.code = "PAGE_TYPE_MISMATCH";
    throw error;
  };
  await assert.rejects(context.receipts.syncCurrentPageWithReceipt("qianchuan"), /当前页不是计划页/);
  const afterFailure = stored.pageSyncReceiptsV1;
  assert.equal(afterFailure.qianchuan.last_success_at, previousSuccess, "a failed attempt must retain the last success time");
  assert.equal(afterFailure.qianchuan.status, "error");
  assert.equal(afterFailure.qianchuan.error_code, "PAGE_TYPE_MISMATCH");
  assert.equal(afterFailure.doudian.status, "success", "qianchuan failure must not contaminate doudian status");

  assert.match(background, /stored\[PAGE_SYNC_RECEIPTS_KEY\]\?\.\[source\]/);
  assert.doesNotMatch(background, /lastSyncValue = stored\.lastSuccessfulSync/);
  console.log("page sync receipt tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
