const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "sidepanel.html"), "utf8");
const script = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");

function sourceBetween(startMarker, endMarker, message) {
  const start = html.indexOf(startMarker);
  const end = html.indexOf(endMarker, start + startMarker.length);
  assert.ok(start >= 0 && end > start, message || `unable to find ${startMarker}`);
  return html.slice(start, end);
}

function openingTagById(id) {
  const match = html.match(new RegExp(`<([a-z][\\w-]*)\\b[^>]*\\bid="${id}"[^>]*>`, "i"));
  assert.ok(match, `missing element #${id}`);
  return match[0];
}

function attribute(source, name) {
  return source.match(new RegExp(`\\b${name}="([^"]*)"`))?.[1] || "";
}

// The daily queue is the operating surface. Product capability evidence stays
// below the actual tasks and is collapsed until the user asks for detail.
const todayCenter = sourceBetween(
  'id="today-task-center"',
  'id="growth-section"',
  "today task center should precede the growth section",
);
const managerTasksIndex = todayCenter.indexOf('id="manager-tasks"');
const capabilityCardIndex = todayCenter.indexOf('id="product-capability-card"');
assert.ok(managerTasksIndex >= 0, "today task center must render manager tasks");
assert.ok(capabilityCardIndex > managerTasksIndex, "manager tasks must precede capability details");
const capabilityTag = openingTagById("product-capability-card");
assert.match(capabilityTag, /^<details\b/i, "capability evidence should use progressive disclosure");
assert.doesNotMatch(capabilityTag, /\sopen(?:\s|=|>)/i, "capability evidence must be collapsed by default");
assert.match(todayCenter, /<small>系统能力与证据<\/small>/);
assert.match(todayCenter, /需要时展开查看，不阻挡今日任务/);

// The default workspace starts from the current Doudian page. Technical store
// identity remains a hidden compatibility surface and is not a user journey.
const systemNavigation = sourceBetween(
  'data-nav-tree="system"',
  "</nav>",
  "system navigation should exist",
);
assert.doesNotMatch(systemNavigation, /data-workspace-key="store-connection"/);
assert.doesNotMatch(systemNavigation, /data-workspace-target="connection-guide"/);
assert.doesNotMatch(
  systemNavigation,
  /data-workspace-target="oceanengine-oauth-card"/,
  "official API authorization should not become a first-run navigation step",
);

const compatibilityTag = openingTagById("connection-guide");
assert.match(compatibilityTag, /\shidden(?:\s|>)/i, "legacy connection DOM must stay hidden");
assert.match(compatibilityTag, /\saria-hidden="true"/i);
for (const id of [
  "connection-center-store",
  "connection-center-account",
  "connection-center-auth",
  "connection-center-scope",
  "connection-center-freshness",
]) {
  assert.match(html, new RegExp(`id="${id}"`), `${id} must remain available to compatibility code`);
  assert.match(script, new RegExp(`getElementById\\("${id}"\\)`), `${id} must be populated by the UI`);
}

const simpleStart = sourceBetween(
  'id="simple-start"',
  'id="connection"',
  "the simple start card should precede technical connection state",
);
assert.match(simpleStart, /打开抖店，直接开始/);
assert.match(simpleStart, /自动确认并巡店/);

const topbar = sourceBetween(
  '<header class="workspace-topbar">',
  '<main class="panel-shell">',
  "workspace topbar should exist",
);
assert.doesNotMatch(topbar, /workspace-context-button/);
assert.doesNotMatch(topbar, /workspace-account-shortcut/);
assert.doesNotMatch(topbar, /data-workspace-target="connection-guide"/);

// Promotion management is one child journey under live operations. Keep the
// order stable so operators move from risk to plans, the bounded real-write
// entry, shadow mode, execution, and finally readback/audit records.
const liveNavigation = sourceBetween(
  'class="workspace-nav-children live-workspace-children"',
  "</div>",
  "live and promotion child navigation should exist",
);
const promotionRoutes = [...liveNavigation.matchAll(/<button\b([^>]*)>\s*<strong>([^<]+)<\/strong>\s*<\/button>/g)]
  .map((match) => ({
    key: attribute(match[1], "data-workspace-key"),
    target: attribute(match[1], "data-workspace-target"),
    label: match[2].trim(),
  }));
assert.deepEqual(promotionRoutes, [
  { key: "live-overview", target: "live-plan-management-section", label: "直播风险" },
  { key: "promotion-overview", target: "promotion-plan-center", label: "全部投放计划" },
  { key: "production-budget-decrease", target: "chengfang-production-write", label: "真实降预算" },
  { key: "autopilot-strategy", target: "autopilot-center", label: "影子托管" },
  { key: "controlled-execution", target: "automation-section", label: "执行准备" },
  { key: "promotion-operation-log", target: "promotion-operation-log", label: "操作记录" },
]);
assert.doesNotMatch(html, /data-nav-tree="promotion"/);
assert.doesNotMatch(html, /<span class="workspace-nav-group">投放<\/span>/);

// Duplicate ids silently break getElementById-based routing and rendering.
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
const counts = new Map();
for (const id of ids) counts.set(id, (counts.get(id) || 0) + 1);
const duplicateIds = [...counts.entries()]
  .filter(([, count]) => count > 1)
  .map(([id]) => id)
  .sort();
assert.deepEqual(duplicateIds, [], `duplicate DOM ids: ${duplicateIds.join(", ")}`);

console.log("v4.10 product layout contract tests passed");
