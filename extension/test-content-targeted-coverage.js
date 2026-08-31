const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "content-common.js"), "utf8");

function visible(extra = {}) {
  return { getClientRects: () => [1], ...extra };
}

function cell(innerText) {
  return visible({ innerText, getAttribute: () => null, querySelectorAll: () => [] });
}

const headers = [cell("\u63a8\u5e7f\u8ba1\u5212 ID"), cell("\u6295\u653e\u72b6\u6001")];
const targetRow = [cell("plan_target_778899"), cell("\u6295\u653e\u4e2d")];
function row(cells, isHeader = false) {
  return visible({
    querySelectorAll(selector) {
      if (selector === "th, [role='columnheader']") return isHeader ? cells : [];
      return cells;
    },
  });
}
const table = visible({
  getAttribute: () => null,
  closest: () => null,
  querySelectorAll(selector) {
    if (selector === "th, [role='columnheader']") return headers;
    if (selector.startsWith("tr,")) return [row(headers, true), row(targetRow)];
    return [];
  },
});
const nextButton = visible({
  disabled: false,
  className: "",
  getAttribute: () => null,
  click() { throw new Error("targeted evidence must stop before visiting the next page"); },
});
const document = {
  title: "Qianchuan campaign management",
  body: { innerText: "plan_target_778899" },
  querySelectorAll(selector) {
    if (selector.startsWith("table, [role='table']")) return [table];
    if (selector.startsWith("button[aria-label*=")) return [nextButton];
    return [];
  },
};
const sandbox = {
  document,
  location: { origin: "https://qianchuan.jinritemai.com", pathname: "/campaigns" },
  getComputedStyle: () => ({ display: "block", visibility: "visible" }),
  setTimeout(callback) { callback(); return 1; },
  clearTimeout() {},
  console,
};
sandbox.globalThis = sandbox;
vm.runInNewContext(source, sandbox, { filename: "content-common-targeted-coverage.vm.js" });

(async () => {
  const snapshot = await sandbox.DianAgentExtractor.collect(
    "qianchuan",
    "campaigns",
    false,
    "full-scan-list-post-execution-readback-1",
    { targetPlanId: "plan_target_778899" },
  );

  assert.equal(snapshot.quality.target_plan_found, true, "the target row must be retained as execution evidence");
  assert.equal(snapshot.quality.targeted, true, "an early-stop target lookup must identify itself as targeted evidence");
  assert.equal(snapshot.quality.scope, "target_plan");
  assert.equal(snapshot.quality.coverage_complete, false, "finding one target is not complete list coverage");
  assert.equal(snapshot.quality.collection_complete, false, "target early-stop must never claim a complete collection");
  console.log("content targeted coverage tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
