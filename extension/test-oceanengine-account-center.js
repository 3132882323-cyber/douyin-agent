const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const center = require("./oceanengine-account-center.js");

const accountKey = "adacct_v1_0123456789abcdef0123456789";
const payload = {
  schema_version: 1,
  platform_write_enabled: false,
  automatic_batch_submit: false,
  secrets_exposed: false,
  accounts: [
    {
      account_key: accountKey,
      display_name: "北区直播账户",
      platform_name: "平台账户",
      masked_id: "•••• 3456",
      advertiser_count: 2,
      linked_stores: [{ key: "store-safe", label: "北区店铺" }],
      token: { state: "active", label: "授权有效", expires_at: 2_100_000_000 },
      sync: {
        synced_at: 2_000_000_000,
        freshness: { state: "fresh", label: "数据新鲜" },
        endpoint_count: 5,
        success_count: 5,
        failure_count: 0,
      },
      capabilities: [
        { id: "plan_read", label: "计划读取", state: "verified", status_label: "读取已验证", verified: true },
        { id: "production_write", label: "真实投放写入", state: "blocked", status_label: "生产写入未开放", verified: false },
      ],
      preferences: { group_name: "直播组", sync_enabled: true, managed: true },
      state: "ready",
    },
  ],
};

const normalized = center.normalize(payload);
assert.equal(normalized.safe, true);
assert.equal(normalized.accounts.length, 1);
assert.equal(normalized.summary.managed, 1);
assert.equal(normalized.summary.advertisers, 2);
assert.equal(normalized.platform_write_enabled, false);
assert.equal(normalized.accounts[0].capabilities.find((item) => item.id === "production_write").verified, false);

assert.equal(center.filterAccounts(normalized.accounts, { query: "北区店铺" }).length, 1);
assert.equal(center.filterAccounts(normalized.accounts, { status: "needs_sync" }).length, 0);
assert.equal(center.filterAccounts(normalized.accounts, { group: "直播组" }).length, 1);

const preference = center.preferencePayload(accountKey, {
  alias: "  新 账户  ", group_name: "  核心组 ", sync_enabled: false, managed: true,
});
assert.equal(preference.alias, "新 账户");
assert.equal(preference.group_name, "核心组");
assert.equal(preference.managed, false, "paused sync must disable local management");

const selection = center.selectionSummary(normalized.accounts, [accountKey], "budget_decrease");
assert.equal(selection.account_count, 1);
assert.equal(selection.advertiser_count, 2);
assert.equal(selection.action_label, "降低预算预演");
assert.equal(selection.platform_write_enabled, false);

const unsafe = center.normalize({ ...payload, platform_write_enabled: true });
assert.equal(unsafe.safe, false);
assert.equal(unsafe.accounts.length, 0, "unsafe backend claims must fail closed");

const html = fs.readFileSync(path.join(__dirname, "sidepanel.html"), "utf8");
const script = fs.readFileSync(path.join(__dirname, "sidepanel.js"), "utf8");
assert.match(html, /id="oceanengine-account-center"/);
assert.match(html, /id="oauth-center-preview"/);
assert.match(html, /id="oauth-center-run-sync"/);
assert.match(html, /<script src="oceanengine-account-center\.js"><\/script>\s*<script src="material-governance\.js"><\/script>/);
assert.match(script, /function renderOceanEngineAccountCenter\(/);
assert.match(script, /\/oauth\/oceanengine\/account-center\/preview/);
assert.match(script, /platform_write_enabled/);
assert.match(script, /oceanengineStatusPollInFlight/);
assert.match(script, /generation !== oceanengineStatusRefreshGeneration/);
assert.match(script, /epoch !== oceanengineStatusPollEpoch \|\| status\.stale/);

const pollingStart = script.indexOf("function stopOceanEngineStatusPolling");
const pollingEnd = script.indexOf("\nfunction scopedSelectedAccountKey", pollingStart);
assert.ok(pollingStart >= 0 && pollingEnd > pollingStart, "OAuth polling implementation should be extractable");

async function testPollingEpochIsolation() {
  const pending = [];
  const timers = new Map();
  const cleared = [];
  let timerSequence = 0;
  const sandbox = {
    oceanengineStatusPoller: null,
    oceanengineStatusPollInFlight: false,
    oceanengineStatusPollEpoch: 0,
    oceanengineStatusRefreshGeneration: 0,
    bridgeFetch(route) {
      return new Promise((resolve) => pending.push({ route, resolve }));
    },
    renderOceanEngineOAuth() {},
    renderOceanEngineSync() {},
    renderOceanEngineAccountCenter() {},
    setInterval(callback) {
      const timerId = ++timerSequence;
      timers.set(timerId, callback);
      return timerId;
    },
    clearInterval(timerId) {
      cleared.push(timerId);
      timers.delete(timerId);
    },
  };
  sandbox.globalThis = sandbox;
  vm.runInNewContext(
    `${script.slice(pollingStart, pollingEnd)}\n` +
      "globalThis.__polling = { startOceanEngineStatusPolling, state: () => ({ poller: oceanengineStatusPoller, epoch: oceanengineStatusPollEpoch }) };",
    sandbox,
    { filename: "oceanengine-polling.vm.js" },
  );

  sandbox.__polling.startOceanEngineStatusPolling();
  const firstPoller = sandbox.__polling.state().poller;
  const stalePoll = timers.get(firstPoller)();
  sandbox.__polling.startOceanEngineStatusPolling();
  const secondPoller = sandbox.__polling.state().poller;
  assert.notEqual(secondPoller, firstPoller);

  pending.splice(0).forEach(({ route, resolve }) => resolve(
    route.endsWith("/status")
      ? { connected: true, authorization_in_progress: false }
      : {},
  ));
  await stalePoll;
  assert.equal(sandbox.__polling.state().poller, secondPoller, "an old epoch must not clear the replacement poller");
  assert.equal(timers.has(secondPoller), true, "the replacement timer must remain scheduled");
  assert.deepEqual(cleared, [firstPoller], "only the superseded timer may be cleared by restart");

  const currentPoll = timers.get(secondPoller)();
  pending.splice(0).forEach(({ route, resolve }) => resolve(
    route.endsWith("/status")
      ? { connected: true, authorization_in_progress: false }
      : {},
  ));
  await currentPoll;
  assert.equal(sandbox.__polling.state().poller, null, "the owning epoch should stop after authorization succeeds");
  assert.equal(timers.has(secondPoller), false);
}

testPollingEpochIsolation()
  .then(() => console.log("oceanengine-account-center tests passed"))
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
