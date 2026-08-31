const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");
const blockStart = script.indexOf("function normalizeTargetedScanReturn");
const blockEnd = script.indexOf("\nfunction agentWriteBlockReason", blockStart);
assert.ok(blockStart >= 0 && blockEnd > blockStart, "targeted marker implementation should be extractable");

function createSandbox(marker, options = {}) {
  const state = { removeCount: 0, navigationCount: 0, getCount: 0 };
  const sandbox = {
    dashboardLoadIsCurrent: options.dashboardLoadIsCurrent || (() => true),
    chrome: {
      storage: {
        local: {
          async get() {
            state.getCount += 1;
            return { scanReturnTarget: options.markerForRead?.(state.getCount) || marker };
          },
          async remove() { state.removeCount += 1; },
        },
      },
    },
    navigateToWorkspaceElement() { state.navigationCount += 1; },
    document: { getElementById: () => ({ textContent: "" }) },
  };
  vm.runInNewContext(script.slice(blockStart, blockEnd), sandbox);
  return { sandbox, state };
}

test("an older terminal scan cannot clear a newer run marker", async () => {
  const marker = { target_id: "product-operating-graph", run_id: "run-new", scope: "product_graph" };
  const { sandbox, state } = createSandbox(marker);
  const result = await sandbox.maybeReturnFromTargetedScan({
    status: "completed",
    run_id: "run-old",
    scope: "product_graph",
  }, 4);
  assert.equal(result, false);
  assert.equal(state.removeCount, 0);
  assert.equal(state.navigationCount, 0);
});

test("a stale dashboard generation cannot clear a matching marker", async () => {
  const marker = { target_id: "product-operating-graph", run_id: "run-1", scope: "product_graph" };
  const { sandbox, state } = createSandbox(marker, { dashboardLoadIsCurrent: () => false });
  const result = await sandbox.maybeReturnFromTargetedScan({
    status: "completed",
    run_id: "run-1",
    scope: "product_graph",
  }, 3);
  assert.equal(result, false);
  assert.equal(state.removeCount, 0);
});

test("only the same run in the current generation consumes its marker", async () => {
  const marker = { target_id: "product-operating-graph", run_id: "run-1", scope: "product_graph" };
  const { sandbox, state } = createSandbox(marker);
  const result = await sandbox.maybeReturnFromTargetedScan({
    status: "completed",
    run_id: "run-1",
    scope: "product_graph",
  }, 5);
  assert.equal(result, true);
  assert.equal(state.getCount, 2);
  assert.equal(state.removeCount, 1);
  assert.equal(state.navigationCount, 1);
});

test("a marker replaced between reads is not consumed", async () => {
  const first = { target_id: "product-operating-graph", run_id: "run-1", scope: "product_graph" };
  const second = { target_id: "product-operating-graph", run_id: "run-2", scope: "product_graph" };
  const { sandbox, state } = createSandbox(first, {
    markerForRead: (count) => count === 1 ? first : second,
  });
  const result = await sandbox.maybeReturnFromTargetedScan({
    status: "completed",
    run_id: "run-1",
    scope: "product_graph",
  }, 5);
  assert.equal(result, false);
  assert.equal(state.removeCount, 0);
});

test("a claimed marker still completes navigation when a newer refresh starts during removal", async () => {
  const marker = { target_id: "product-operating-graph", run_id: "run-1", scope: "product_graph" };
  let generationChecks = 0;
  const { sandbox, state } = createSandbox(marker, {
    dashboardLoadIsCurrent: () => {
      generationChecks += 1;
      return generationChecks <= 2;
    },
  });
  const result = await sandbox.maybeReturnFromTargetedScan({
    status: "completed",
    run_id: "run-1",
    scope: "product_graph",
  }, 5);
  assert.equal(result, true);
  assert.equal(state.removeCount, 1);
  assert.equal(state.navigationCount, 1, "claimed one-shot navigation must not be lost after deleting its marker");
  assert.equal(generationChecks, 2, "generation ownership is decided before marker removal");
});
