const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const background = fs.readFileSync(path.join(__dirname, "background.js"), "utf8");
const blockStart = background.indexOf("async function syncSource");
const blockEnd = background.indexOf("\nasync function syncAll", blockStart);
assert.ok(blockStart >= 0 && blockEnd > blockStart, "syncSource should be extractable");

function loadSyncSource(responses) {
  const calls = { collect: [], status: [] };
  const tabs = responses.map((_, index) => ({ id: index + 1, url: `https://fxg.jinritemai.com/page-${index + 1}` }));
  const context = {
    querySourceTabs: async () => tabs,
    collectFromTab: async (source, tab, reason) => {
      calls.collect.push({ source, tab, reason });
      return responses[tab.id - 1];
    },
    updateStatus: async (...args) => { calls.status.push(args); },
  };
  vm.runInNewContext(
    `${background.slice(blockStart, blockEnd)}\nglobalThis.__syncSource = syncSource;`,
    context,
    { filename: "background-sync-source.vm.js" },
  );
  return { syncSource: context.__syncSource, calls };
}

(async () => {
  {
    const runtime = loadSyncSource([{
      ok: true,
      bridge: { ok: false, error_code: "STORE_IDENTITY_UNRESOLVED", error: "candidate rejected" },
    }]);
    const result = await runtime.syncSource("doudian", "manual");
    assert.equal(result.collected, 0, "outer content-script success must not count a rejected bridge candidate");
    assert.deepEqual(Array.from(result.errors), ["candidate rejected"]);
    assert.equal(runtime.calls.status.at(-1)[2], "error");
    assert.notEqual(runtime.calls.status.at(-1)[2], "ok");
  }

  {
    const runtime = loadSyncSource([
      { ok: true, bridge: { ok: true } },
      { ok: true, bridge: { ok: false, error: "second candidate rejected" } },
    ]);
    const result = await runtime.syncSource("doudian", "manual");
    assert.equal(result.collected, 1);
    assert.equal(result.errors.length, 1);
    assert.equal(runtime.calls.status.at(-1)[2], "warning", "partial bridge rejection must not be labelled fully successful");
  }

  console.log("background sync-source bridge acceptance tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
