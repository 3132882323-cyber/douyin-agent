const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "sidepanel.html"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "sidepanel.css"), "utf8");
const script = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");

const navTrees = [...html.matchAll(/data-nav-tree="([^"]+)"/g)].map((match) => match[1]);
assert.deepEqual(navTrees, ["system"]);

for (const target of [
  "today-task-center",
  "product-operating-graph",
  "live-plan-management-section",
  "content-workbench",
  "promotion-plan-center",
  "autopilot-center",
  "promotion-operation-log",
  "scan-card",
  "report-center-card",
]) {
  assert.match(html, new RegExp(`data-workspace-target="${target}"`), `missing user route for ${target}`);
}
assert.doesNotMatch(html, /data-workspace-target="connection-guide"/);
assert.match(html, /<section id="connection-guide"[^>]*\shidden\s+aria-hidden="true"[^>]*>/);
assert.match(html, /id="simple-start"[\s\S]*打开抖店，直接开始[\s\S]*自动确认并巡店/);
assert.match(script, /promotion-plan-open-bulk[\s\S]*?navigateToWorkspaceElement\("promotion-bulk-action-center"/);
assert.match(script, /function openSimpleBatchPlan\([\s\S]*?navigateToWorkspaceElement\("batch-plan-preparation"/);

const pathStages = [...html.matchAll(/data-workspace-path-stage="([^"]+)"/g)].map((match) => match[1]);
assert.deepEqual(pathStages, ["prepare", "diagnose", "act", "verify"]);
assert.match(html, /id="workspace-user-path-next"[^>]*data-workspace-target="next-best-action"/);

const toolbar = html.match(/<div class="promotion-plan-toolbar">([\s\S]*?)<\/div>\s*<details id="promotion-plan-advanced-filters"/);
assert.ok(toolbar, "basic plan toolbar should precede advanced filters");
assert.match(toolbar[1], /promotion-plan-account-filter/);
assert.match(toolbar[1], /promotion-plan-mode-filter/);
assert.match(toolbar[1], /promotion-plan-type-filter/);
assert.doesNotMatch(toolbar[1], /promotion-plan-status-filter|promotion-plan-binding-filter/);

const advanced = html.match(/<details id="promotion-plan-advanced-filters"[\s\S]*?<\/details>/);
assert.ok(advanced, "advanced filters should use progressive disclosure");
assert.match(advanced[0], /promotion-plan-status-filter/);
assert.match(advanced[0], /promotion-plan-binding-filter/);
assert.match(advanced[0], /promotion-plan-feature-filters/);

assert.match(script, /const ROLE_DEFAULT_TARGET = Object\.freeze/);
assert.match(script, /const WORKSPACE_PATH_ORDER = Object\.freeze\(\["prepare", "diagnose", "act", "verify"\]\)/);
assert.match(script, /function renderWorkspaceUserPath\(/);
assert.match(script, /\.workspace-subfocus-hidden/);
assert.match(script, /let currentWorkspaceTarget = "today-task-center"/);
assert.match(script, /if \(todayRoute\) navigateWorkspaceTarget\(todayRoute\)/);
assert.match(script, /element\.id === "priority-reminder"/);
assert.match(script, /async function runQuickScan\(/);
assert.match(script, /async function ensureCurrentStoreForScan\(/);
assert.match(script, /await ensureCurrentStoreForScan\(/);
assert.match(script, /scan_scope:\s*"quick"/);
assert.doesNotMatch(script, /if \(selectedStoreKey && !scanButton\.disabled\) scanButton\.click\(\)/);

assert.match(css, /\.workspace-user-path\s*\{/);
assert.match(css, /\.workspace-subfocus-hidden\s*\{\s*display:\s*none\s*!important/);
assert.match(css, /\.promotion-plan-advanced-filter-grid\s*\{/);

console.log("user-centered navigation tests passed");
