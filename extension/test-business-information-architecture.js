const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "sidepanel.html"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "sidepanel.css"), "utf8");
const script = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");

const roleButtons = [...html.matchAll(/<button[^>]*data-role="([^"]+)"[^>]*data-workspace-target="([^"]+)"[^>]*>[\s\S]*?<strong>([^<]+)<\/strong><\/button>/g)]
  .map((match) => ({ role: match[1], target: match[2], label: match[3] }));
assert.deepEqual(roleButtons, [
  { role: "货架商品", target: "product-operating-graph", label: "商品经营" },
  { role: "直播投放", target: "live-plan-management-section", label: "直播与投放" },
  { role: "内容", target: "content-workbench", label: "内容素材" },
]);

assert.doesNotMatch(html, /<span class="workspace-nav-group">投放<\/span>/);
assert.doesNotMatch(html, /data-nav-tree="promotion"/);
assert.match(html, /class="workspace-nav-children live-workspace-children"[\s\S]*?data-workspace-target="live-plan-management-section"[\s\S]*?data-workspace-target="promotion-plan-center"[\s\S]*?data-workspace-target="autopilot-center"[\s\S]*?data-workspace-target="automation-section"[\s\S]*?data-workspace-target="promotion-operation-log"/);
assert.match(html, /id="promotion-plan-batch-shortcut"[^>]*>生成计划草稿<\/button>/);
assert.match(html, /id="promotion-plan-open-bulk"[^>]*>预演所选计划动作<\/button>/);

const productOwners = [...html.matchAll(/<section[^>]*class="[^"]*module-section[^"]*"[^>]*data-owner="货架商品"/g)];
assert.equal(productOwners.length, 1, "商品经营默认面只允许一个商品卡模块");
assert.match(html, /id="product-operating-graph"[^>]*data-owner="货架商品"/);
assert.match(html, /id="shelf-business-center"[^>]*data-owner=""/);
assert.match(html, /class="module-section inventory-section legacy-product-detail" data-owner=""/);
assert.match(html, /id="product-operating-graph"[\s\S]*?<h3 id="product-operating-graph-title">商品卡<\/h3>[\s\S]*?id="product-graph-list"/);

const contentOwners = [...html.matchAll(/<section[^>]*class="[^"]*module-section[^"]*"[^>]*data-owner="内容"/g)];
assert.equal(contentOwners.length, 1, "内容素材默认面只允许一个素材模块");
assert.match(html, /id="content-workbench"[\s\S]*?<h3 id="material-library-title">素材库<\/h3>[\s\S]*?id="creative-videos"/);
assert.match(html, /class="business-support" hidden aria-hidden="true"/);

for (const id of [
  "live-plan-management-section",
  "promotion-plan-center",
  "promotion-bulk-action-center",
  "promotion-operation-log",
  "promotion-mode-workbench",
  "automation-section",
  "product-plan-management-section",
  "batch-plan-preparation",
]) {
  assert.match(html, new RegExp(`id="${id}"[^>]*data-owner="直播投放"`), `${id} must belong to live operations`);
}

assert.match(script, /"直播投放": "live-plan-management-section"/);
assert.match(script, /const businessModule = Boolean\(target\.closest\?\.\("\.module-section"\)\)/);
assert.match(script, /navigateWorkspaceTarget\(button\);/);
assert.match(css, /#role-nav > \[data-role="直播投放"\]\.active \+ \.live-workspace-children/);
assert.match(css, /\.business-support\[hidden\]\s*\{\s*display:\s*none\s*!important/);

const briefStart = script.indexOf("latestBrief = [");
const briefEnd = script.indexOf("].filter(Boolean).join", briefStart);
const briefSource = script.slice(briefStart, briefEnd);
assert.ok(briefStart >= 0 && briefEnd > briefStart, "brief export source must remain discoverable");
assert.match(briefSource, /今日任务/);
assert.doesNotMatch(briefSource, /总管/);

console.log("business information architecture tests passed");
