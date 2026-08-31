"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const simple = require("./simple-experience.js");

const script = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");

function extract(startText, endText) {
  const start = script.indexOf(startText);
  const end = script.indexOf(endText, start);
  assert.ok(start >= 0 && end > start, `cannot extract ${startText}`);
  return script.slice(start, end);
}

function classList() {
  const values = new Set();
  return {
    toggle(name, force) {
      if (force === false) values.delete(name);
      else if (force === true || !values.has(name)) values.add(name);
      else values.delete(name);
    },
    add(name) { values.add(name); },
    remove(name) { values.delete(name); },
    contains(name) { return values.has(name); },
  };
}

const entryCalls = { focus: [], navigate: [], configure: [] };
const onboardingCard = { id: "simple-start", classList: classList(), scrollIntoView() {} };
const todayRoute = { id: "today-route" };
const entryElements = {
  "simple-start": onboardingCard,
  "workspace-current-title": { textContent: "" },
  "workspace-page-title": { textContent: "" },
  "workspace-page-subtitle": { textContent: "" },
};
const entryContext = {
  console,
  DianSimpleExperience: simple,
  setTimeout() {},
  CHENGFANG_DEMO_ENTRY_ROUTE: "chengfang-demo",
  experienceMode: "simple",
  currentSimpleJourney: { complete: false },
  currentWorkspaceTarget: "today-task-center",
  currentRole: "货架商品",
  document: {
    getElementById(id) { return entryElements[id] || null; },
    querySelector(selector) { return selector.includes("today-tasks") ? todayRoute : null; },
    querySelectorAll() { return []; },
  },
  clearWorkspacePageFocus() {},
  focusWorkspacePage(node) { entryCalls.focus.push(node?.id); },
  configureWorkspacePrimaryAction(target) { entryCalls.configure.push(target); },
  navigateWorkspaceTarget(node) { entryCalls.navigate.push(node?.id); },
  renderWorkbench() {},
  applyModuleVisibility() {},
  setPromotionView() {},
  focusWorkspacePageTarget() {},
};
vm.runInNewContext(
  `${extract("function focusWorkbenchEntryRoute", "\nconst ROLE_WORKBENCH")}; globalThis.__focusEntry = focusWorkbenchEntryRoute;`,
  entryContext,
  { filename: "simple-entry-focus.vm.js" },
);

entryContext.__focusEntry("");
assert.deepEqual(entryCalls.focus, ["simple-start"]);
assert.deepEqual(entryCalls.navigate, []);
assert.deepEqual(entryCalls.configure, ["simple-start"]);
assert.equal(entryElements["workspace-page-title"].textContent, "完成当前唯一一步");

entryContext.currentSimpleJourney = { complete: true };
vm.runInNewContext("currentSimpleJourney = { complete: true }; globalThis.__focusEntry('');", entryContext);
assert.deepEqual(entryCalls.navigate, ["today-route"]);

const actionButton = { textContent: "", dataset: {}, disabled: false, title: "", removeAttribute(name) { delete this[name]; } };
const ctaContext = {
  currentJourneyCommand: {
    detail: "数据过期",
    primaryAction: { id: "refresh_core_data", kind: "scan", label: "刷新 2 个核心经营页", reason: "只刷新过期页" },
  },
  document: { getElementById: () => actionButton },
};
vm.runInNewContext(
  `${extract("function configureWorkspacePrimaryAction", "\nfunction setWorkspaceNavTreeExpanded")}; globalThis.__configure = configureWorkspacePrimaryAction;`,
  ctaContext,
  { filename: "journey-primary-cta.vm.js" },
);
ctaContext.__configure("product-operating-graph");
assert.equal(actionButton.textContent, "刷新 2 个核心经营页");
assert.equal(actionButton.dataset.delegateTarget, "journey-primary-action");
assert.equal(actionButton.dataset.journeyActionId, "refresh_core_data");

const panel = { classList: classList(), dataset: {} };
const emptyElements = {
  "next-best-action": panel,
  "next-best-action-title": { textContent: "" },
  "next-best-action-detail": { textContent: "" },
  "next-best-action-button": { textContent: "", dataset: {}, disabled: false },
};
const emptyContext = {
  DianSimpleExperience: simple,
  currentOps: {},
  currentDashboardFailures: [],
  currentJourneyCommand: { dataState: "stale" },
  currentSimpleJourney: null,
  document: { getElementById(id) { return emptyElements[id]; } },
  observationPrimaryAction() { throw new Error("must not inspect an observation when the queue is empty"); },
};
vm.runInNewContext(
  `${extract("function renderNextBestAction", "\nfunction renderPriorityReminder")}; globalThis.__renderEmpty = renderNextBestAction;`,
  emptyContext,
  { filename: "simple-empty-state.vm.js" },
);
emptyContext.__renderEmpty([], {});
assert.equal(panel.dataset.emptyState, "stale");
assert.match(emptyElements["next-best-action-title"].textContent, /过期/);
assert.match(emptyElements["next-best-action-detail"].textContent, /不能证明当前没有问题/);
assert.equal(emptyElements["next-best-action-button"].dataset.emptyAction, "refresh_core_data");

vm.runInNewContext(
  "currentJourneyCommand = { dataState: 'fresh' }; currentDashboardFailures = ['今日任务']; globalThis.__renderEmpty([], {});",
  emptyContext,
);
assert.equal(panel.dataset.emptyState, "failed");
assert.match(emptyElements["next-best-action-title"].textContent, /读取失败/);

console.log("simple entry and empty-state DOM tests passed");
