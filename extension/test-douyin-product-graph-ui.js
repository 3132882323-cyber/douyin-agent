const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "sidepanel.html"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "sidepanel.css"), "utf8");
const script = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");
const background = fs.readFileSync(path.join(__dirname, "background.js"), "utf8");

assert.match(html, /data-role="货架商品"[^>]*data-workspace-target="product-operating-graph"/);
assert.match(
  html,
  /<section id="product-operating-graph"[^>]*data-owner="货架商品"[\s\S]*?<h3 id="product-operating-graph-title">商品卡<\/h3>[\s\S]*?<div id="product-graph-list"/,
  "product cards should be the only default product-operating view",
);
assert.match(html, /id="shelf-business-center"[^>]*data-owner=""/);
for (const id of ["product-operating-graph", "product-graph-status", "product-graph-metrics", "product-graph-readiness", "product-graph-list", "product-graph-note", "product-graph-collect"]) {
  assert.equal((html.match(new RegExp(`\\bid="${id}"`, "g")) || []).length, 1, `${id} must be unique`);
}

assert.match(script, /function renderProductOperatingGraph\(graph = \{\}\)/);
assert.match(script, /function renderProductGraphReadiness\(readiness = \{\}, products = \[\]\)/);
assert.match(script, /function productGraphMetric\(product = \{\}, metric = ""\)/);
assert.match(script, /当前禁止直接放量/);
assert.match(script, /同名商品强行关联/);
assert.match(script, /"货架商品": "product-operating-graph"/);
assert.match(script, /workspaceKey === "shelf-products"/);
assert.match(script, /scan_scope: "product_graph"/);
assert.match(script, /scanReturnTarget:\s*\{[\s\S]{0,180}target_id: "product-operating-graph"[\s\S]{0,180}run_id: startedRunId[\s\S]{0,180}scope: "product_graph"/);
assert.match(script, /response\.started !== true \|\| !response\.run_id/);
assert.match(script, /function maybeReturnFromTargetedScan\(scan = \{\}, generation = 0\)/);
assert.match(script, /targetedScanReturnMatches\(scan, target\)/);
assert.doesNotMatch(script, /if \(stored\.scanReturnTarget\) await chrome\.storage\.local\.remove\("scanReturnTarget"\)/);
assert.match(script, /await loadDashboard\(\);[\s\S]*maybeReturnFromTargetedScan/);
assert.match(script, /pageIds = \(Array\.isArray\(pageIds\)/);
assert.match(script, /const identifier = product\.product_id \|\| product\.sku_id \|\| product\.merchant_code/);
assert.match(script, /readiness\.status === "base_ready" && products\.length > 0/);
assert.match(script, /button\.textContent = fullScanRunning \? "正在同步商品…" : "同步商品"/);
assert.match(script, /button\.dataset\.actionKind = actionKind/);
assert.match(script, /if \(kind === "focus_product"\)/);
assert.match(script, /card\.id = "product-graph-first-recommendation"/);
assert.match(background, /const productGraphScope = scanScope === "product_graph"/);
assert.match(background, /const resolvedScope = productGraphScope \? "product_graph"/);

const targetStart = script.indexOf("function normalizeTargetedScanReturn");
const targetEnd = script.indexOf("\nasync function maybeReturnFromTargetedScan", targetStart);
assert.ok(targetStart >= 0 && targetEnd > targetStart, "targeted scan return helpers must be extractable");
const targetSandbox = {};
vm.runInNewContext(script.slice(targetStart, targetEnd), targetSandbox);
const target = { target_id: "product-operating-graph", run_id: "run-product-1", scope: "product_graph" };
assert.equal(targetSandbox.normalizeTargetedScanReturn("product-operating-graph"), null, "legacy string markers must fail closed");
assert.equal(targetSandbox.targetedScanReturnMatches({ run_id: "run-full-1", scope: "full" }, target), false);
assert.equal(targetSandbox.targetedScanReturnMatches({ run_id: "run-product-2", scope: "product_graph" }, target), false);
assert.equal(targetSandbox.targetedScanReturnMatches({ run_id: "run-product-1", scope: "product_graph" }, target), true);

for (const selector of [
  ".product-operating-graph",
  ".product-graph-heading",
  ".product-graph-readiness",
  ".product-graph-readiness-step",
  ".product-graph-list",
  ".product-graph-card",
  ".product-graph-gate.blocked",
]) {
  assert.match(css, new RegExp(selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\s*\\{"), `missing ${selector} style`);
}
assert.match(css, /\.product-graph-heading h3\s*\{[^}]*font-size:\s*18px/s);
assert.match(css, /\.product-graph-heading-actions button,[\s\S]*?min-height:\s*40px/s);
assert.match(css, /\.product-graph-readiness-step strong\s*\{[^}]*font-size:\s*13px/s);

console.log("douyin product graph UI tests passed");
