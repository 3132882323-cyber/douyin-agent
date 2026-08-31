const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const policy = require("./simple-experience.js");

const html = fs.readFileSync(path.join(__dirname, "sidepanel.html"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "sidepanel.css"), "utf8");
const sidepanel = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");
const popup = fs.readFileSync(path.join(__dirname, "popup.html"), "utf8");
const welcome = fs.readFileSync(path.join(__dirname, "welcome.html"), "utf8");
const build = fs.readFileSync(path.join(__dirname, "..", "build_extension.ps1"), "utf8");

assert.match(html, /<body data-experience-mode="simple">/);
assert.match(html, /id="simple-start"/);
assert.equal((html.match(/data-simple-nav/g) || []).length, 5);
assert.match(html, /data-simple-nav[^>]*data-workspace-key="business-report"/);
assert.match(css, /simple-show-report/);
assert.match(html, /data-workspace-key="industry-knowledge"[\s\S]*data-workspace-target="industry-pack-center"/);
assert.equal((html.match(/data-experience-mode-value=/g) || []).length, 2);
assert.match(html, /data-experience-mode-value="professional">完整/);
assert.match(html, /data-experience-mode-value="simple" class="active">专注/);
assert.match(html, /<option value="full_domain">全域推广<\/option>/);
assert.match(html, /<option value="chengfang">乘方推广<\/option>/);
assert.match(html, /<option value="suixintui">随心推<\/option>/);
assert.match(html, /<script src="simple-experience\.js"><\/script>\s*<script src="journey-orchestrator\.js"><\/script>\s*<script src="sidepanel\.js"><\/script>/);
assert.match(css, /body\[data-experience-mode="simple"\]/);
assert.match(css, /simple-show-batch/);
assert.match(css, /simple-show-industry/);
assert.match(css, /simple-show-settings #strategy-settings-card/);
assert.match(css, /simple-show-version #data-version-center/);
assert.match(css, /\.workspace-focus-hidden/);
assert.match(sidepanel, /EXPERIENCE_MODE_KEY = "experienceModeV2"/);
assert.match(sidepanel, /function renderSimpleJourney/);
assert.match(sidepanel, /function focusWorkspacePage/);
assert.match(sidepanel, /function revealSimpleWorkspaceTarget/);
assert.match(sidepanel, /\["simple-show-settings", "strategy-settings-card"\]/);
assert.match(sidepanel, /\["simple-show-version", "data-version-center"\]/);
assert.match(sidepanel, /targetId !== "priority-reminder"/);
assert.match(sidepanel, /navigationButton\?\.matches\?\.\("\[data-simple-nav\]"\)/);
assert.match(build, /"simple-experience\.js"/);
assert.match(popup, /class="popup-next-step"[\s\S]*id="sync-button"/);
assert.match(popup, /<details class="popup-more">/);
assert.match(welcome, /只需要：打开抖店 → 开始巡店 → 看建议/);
assert.match(welcome, /<details class="more">/);
assert.match(html, /<section id="connection-guide"[^>]*\shidden\s+aria-hidden="true"[^>]*>/);
assert.doesNotMatch(html, /data-workspace-key="store-connection"/);
assert.doesNotMatch(html, /data-workspace-target="connection-guide"/);
assert.doesNotMatch(html, /id="workspace-account-shortcut"|class="workspace-context-button"/);

assert.equal(policy.normalizeMode(), "simple");
assert.equal(policy.normalizeMode("professional"), "professional");
assert.equal(policy.normalizeMode("simple"), "simple");
assert.equal(policy.normalizeMode("unknown"), "simple");

const first = policy.deriveJourney({ steps: [] }, { next_upgrade: { id: "identify_store" } });
assert.equal(first.completed, 0);
assert.equal(first.steps[0].current, true);
assert.deepEqual(first.steps.map((step) => step.label), ["打开抖店", "自动确认", "开始巡店"]);
assert.equal(first.action.id, "start_store_scan");
assert.equal(first.action.label, "打开抖店并开始");
assert.doesNotMatch(first.action.id, /identify|confirm|select/);

const readyForAdvice = policy.deriveJourney({
  store_confirmed: true,
  steps: [
    { id: "store", complete: true },
    { id: "sync", complete: true },
    { id: "first_task", complete: true },
    { id: "evidence", complete: false },
  ],
}, { operational: { state: "data_fresh", core_data_fresh: true } });
assert.equal(readyForAdvice.completed, 3);
assert.equal(readyForAdvice.complete, true);
assert.equal(readyForAdvice.steps.some((step) => step.current), false);
assert.equal(readyForAdvice.action.id, "view_first_task");

const complete = policy.deriveJourney({
  status: "completed",
  store_confirmed: true,
  steps: [{ id: "store", complete: true }, { id: "sync", complete: true }, { id: "evidence", complete: true }],
}, { operational: { state: "data_fresh", core_data_fresh: true } });
assert.equal(complete.complete, true);
assert.equal(complete.completed, 3);
assert.equal(complete.action.id, "view_today");

const historicalL3ButStale = policy.deriveJourney({
  status: "completed",
  store_confirmed: true,
  steps: [{ id: "store", complete: true }, { id: "sync", complete: true }, { id: "evidence", complete: true }],
}, {
  level: "L3",
  operational: {
    state: "refresh_required",
    core_data_fresh: false,
    refresh_page_ids: ["products", "orders", "products"],
  },
  next_upgrade: { id: "refresh_core_data", page_ids: ["overview"] },
});
assert.equal(historicalL3ButStale.complete, false);
assert.equal(historicalL3ButStale.data_state, "stale");
assert.equal(historicalL3ButStale.action.id, "refresh_core_data");
assert.deepEqual(historicalL3ButStale.action.page_ids, ["products", "orders"]);

const failedRefresh = policy.deriveJourney({ store_confirmed: true }, {
  operational: { state: "failed", core_data_fresh: false, refresh_page_ids: ["shelf"] },
});
assert.equal(failedRefresh.action.id, "refresh_core_data");
assert.deepEqual(failedRefresh.action.page_ids, ["shelf"]);
assert.match(failedRefresh.action.detail, /只重试/);

const loading = policy.deriveJourney({ store_confirmed: true }, {
  operational: { state: "loading", core_data_fresh: false },
});
assert.equal(loading.action.id, "none");

assert.equal(policy.onboardingEntryTarget({ mode: "simple", journey: historicalL3ButStale }), "simple-start");
assert.equal(policy.onboardingEntryTarget({ mode: "simple", journey: complete }), "today-task-center");
assert.equal(policy.onboardingEntryTarget({ mode: "professional", journey: historicalL3ButStale }), "today-task-center");
assert.equal(policy.deriveEmptyState({ data_state: "stale" }).id, "stale");
assert.match(policy.deriveEmptyState({ data_state: "stale" }).detail, /不能证明当前没有问题/);
assert.equal(policy.deriveEmptyState({ data_state: "failed" }).id, "failed");
assert.equal(policy.deriveEmptyState({ data_state: "fresh" }).id, "no_actionable_task");
assert.equal(policy.deriveEmptyState({ data_state: "fresh", clean: true }).id, "clean");

console.log("simple experience policy tests passed");
