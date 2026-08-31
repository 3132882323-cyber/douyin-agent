const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const sidepanel = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");
const popup = fs.readFileSync(path.join(__dirname, "popup.js"), "utf8");

class FakeElement {
  constructor() {
    this.textContent = "";
    this.className = "";
    this.disabled = false;
    this.hidden = false;
    this.value = "";
    this.placeholder = "";
    this.children = [];
  }

  replaceChildren(...children) {
    this.children = children;
    this.textContent = "";
  }
}

function fakeDocument(ids) {
  const elements = Object.fromEntries(ids.map((id) => [id, new FakeElement()]));
  return {
    elements,
    document: {
      getElementById(id) { return elements[id] || null; },
      createElement() { return new FakeElement(); },
    },
  };
}

{
  const start = sidepanel.indexOf("function renderOceanEngineOAuth");
  const end = sidepanel.indexOf("\nfunction oceanEngineCenterNode", start);
  assert.ok(start >= 0 && end > start);
  const { document, elements } = fakeDocument([
    "oceanengine-oauth-state", "oceanengine-oauth-result", "oceanengine-app-id",
    "oceanengine-app-secret", "oceanengine-accounts", "sync-oceanengine-data",
    "connection-center-auth", "oceanengine-account-count", "oceanengine-sync-result",
  ]);
  const context = { document, Date };
  vm.runInNewContext(
    `${sidepanel.slice(start, end)}\n` +
      "globalThis.__render = { renderOceanEngineOAuth, renderOceanEngineSync };",
    context,
  );

  context.__render.renderOceanEngineOAuth({
    connected: false,
    needs_refresh: true,
    refresh_available: true,
    secret_saved: true,
    account_count: 1,
    accounts: [],
  });
  assert.equal(elements["sync-oceanengine-data"].disabled, false);
  assert.match(elements["oceanengine-oauth-state"].textContent, /自动续期/);
  assert.match(elements["oceanengine-oauth-result"].textContent, /无需重新授权/);

  context.__render.renderOceanEngineSync({
    ok: false,
    synced_at: null,
    recovery_required: true,
    corruption_detected: true,
    error: { code: "SYNC_STATUS_CORRUPT", message: "saved status invalid" },
  });
  assert.equal(elements["oceanengine-sync-result"].className, "error");
  assert.match(elements["oceanengine-sync-result"].textContent, /同步记录异常/);
  assert.match(elements["oceanengine-sync-result"].textContent, /只读同步恢复/);
}

{
  const start = popup.indexOf("function renderApi(");
  const end = popup.indexOf("\nfunction renderApiUnavailable", start);
  assert.ok(start >= 0 && end > start);
  const { document, elements } = fakeDocument(["api-state", "api-detail", "api-sync-note", "api-sync-button"]);
  const context = { document, relativeTime: () => "刚刚" };
  vm.runInNewContext(`${popup.slice(start, end)}\nglobalThis.__renderApi = renderApi;`, context);
  context.__renderApi(
    { connected: false, refresh_available: true, account_count: 1 },
    { ok: false, recovery_required: true, error: { message: "status invalid" } },
  );
  assert.equal(elements["api-sync-button"].disabled, false);
  assert.equal(elements["api-state"].className, "tag error");
  assert.match(elements["api-sync-note"].textContent, /同步记录异常/);
}

{
  const start = sidepanel.indexOf("function renderHealthMonitor");
  const end = sidepanel.indexOf("\nfunction renderEffectiveness", start);
  assert.ok(start >= 0 && end > start);
  const { document, elements } = fakeDocument(["health-alerts", "health-count"]);
  const context = { document };
  vm.runInNewContext(`${sidepanel.slice(start, end)}\nglobalThis.__renderHealthMonitor = renderHealthMonitor;`, context);
  elements["health-alerts"].textContent = "上一店铺的异常卡片";
  elements["health-alerts"].className = "stack";
  context.__renderHealthMonitor({ alerts: [], baselines: {}, pages_with_baseline: 0 });
  assert.equal(elements["health-alerts"].className, "stack empty-state");
  assert.match(elements["health-alerts"].textContent, /当前店铺尚未建立采集基线/);
}

assert.match(sidepanel, /oceanengineSyncR\.status === "fulfilled" && oceanengineSyncR\.value\?\.ok === false/);
console.log("oceanengine sync stability UI tests passed");
