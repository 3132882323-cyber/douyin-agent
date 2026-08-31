"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "sidepanel.css"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "sidepanel.html"), "utf8");
const start = source.indexOf("function qianchuanSyncAgeLabel");
const end = source.indexOf("\nfunction normalizeQianchuanSyncOptions", start);
assert.ok(start >= 0 && end > start, "floating sync dock policy should be extractable");

const context = {
  Date,
  Math,
  Number,
  String,
  Object,
  Promise,
  QIANCHUAN_SYNC_FRESH_MS: 30 * 60 * 1000,
  LABELS: { campaigns: "商品计划" },
  document: {
    body: { classList: { toggle() {} } },
    getElementById() { return null; },
  },
  chrome: { storage: { local: { async set() {} } } },
  setTimeout() { return 1; },
  clearTimeout() {},
  qianchuanSyncFreshnessTimer: null,
  latestQianchuanSyncUiReceipt: { success: null, attempt: null },
};
vm.runInNewContext(`${source.slice(start, end)}\nthis.policy = { qianchuanSyncAgeLabel, qianchuanSyncIsFresh, mergeQianchuanSyncReceipts };`, context);

const now = Date.parse("2026-08-28T10:00:00+08:00");
assert.equal(context.policy.qianchuanSyncAgeLabel(now - 30_000, now), "刚刚");
assert.equal(context.policy.qianchuanSyncAgeLabel(now - 12 * 60_000, now), "12 分钟前");
assert.equal(context.policy.qianchuanSyncIsFresh(now - 29 * 60_000, now), true);
assert.equal(context.policy.qianchuanSyncIsFresh(now - 31 * 60_000, now), false);

const merged = context.policy.mergeQianchuanSyncReceipts(
  { status: "success", timestamp: 100, page_type: "campaigns" },
  { status: "error", timestamp: 120, message: "旧失败" },
  { status: "success", last_attempt_at: 200, last_success_at: 200, page_type: "campaigns", account_label: "账户 A" },
);
assert.equal(merged.success.timestamp, 200);
assert.equal(merged.success.account_label, "账户 A");
assert.equal(merged.attempt.status, "success", "new authoritative success must replace an older local failure");

assert.match(css, /\.qianchuan-sync-dock-control\s*\{[^}]*bottom:\s*calc\(100% \+ 6px\)/, "dock controls must sit outside, not over, the primary action");
assert.doesNotMatch(css, /\.qianchuan-sync-dock-control\s*\{[^}]*top:\s*-/, "dock controls must not overlap the primary action");
assert.match(css, /@media \(max-width: 520px\)[\s\S]*?\.workspace-context-button\s*\{\s*display:\s*none;/, "narrow topbar must reserve room for the restore menu");
assert.match(html, /<summary aria-label="更多设置" title="更多设置">更多设置<\/summary>/);

console.log("qianchuan sync dock policy tests passed");
