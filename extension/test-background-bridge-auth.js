const assert = require("assert");
const fs = require("fs");
const vm = require("vm");
const auth = require("./bridge-auth.js");
const TEST_VERSION = "4.14.0";

function jsonResponse(status, payload) {
  return {
    status,
    ok: status >= 200 && status < 300,
    async json() { return payload; },
    clone() { return jsonResponse(status, payload); },
  };
}

function memoryStorage() {
  const values = {};
  return {
    values,
    async get(key) { return { [key]: values[key] }; },
    async set(patch) { Object.assign(values, patch); },
    async remove(key) { delete values[key]; },
  };
}

(async () => {
  const calls = [];
  const storage = memoryStorage();
  const client = auth.createClient({
    baseUrl: "http://127.0.0.1:8765",
    extensionId: "trusted_extension",
    extensionVersion: TEST_VERSION,
    storage,
    now: () => 1000,
    fetchImpl: async (url, options = {}) => {
      calls.push({ url, options });
      if (url.endsWith("/auth/session")) {
        return jsonResponse(200, {
          ok: true, access_token: "token_one", token_type: "DianAgent",
          expires_at: 3601000, expires_in: 3600, install_id: "install_a",
          extension_version: TEST_VERSION,
        });
      }
      return jsonResponse(200, { ok: true });
    },
  });
  const response = await client.authorizedFetch("/stores", { method: "GET" });
  assert.strictEqual(response.ok, true);
  assert.strictEqual(calls.length, 2);
  assert.deepStrictEqual(JSON.parse(calls[0].options.body), {
    extension_id: "trusted_extension",
    extension_version: TEST_VERSION,
  });
  assert.strictEqual(calls[0].options.headers["X-Dian-Agent-Extension-Version"], TEST_VERSION);
  assert.strictEqual(calls[0].options.headers[auth.EXTENSION_ID_HEADER], "trusted_extension");
  assert.strictEqual(calls[1].options.headers["X-Dian-Agent-Token"], "token_one");
  assert.strictEqual(calls[1].options.headers[auth.EXTENSION_ID_HEADER], "trusted_extension");
  assert.strictEqual(calls[1].options.headers["X-Dian-Agent-Extension-Version"], TEST_VERSION);

  let sessionNumber = 0;
  let apiNumber = 0;
  const retryCalls = [];
  const retryClient = auth.createClient({
    baseUrl: "http://127.0.0.1:8765",
    extensionId: "trusted_extension",
    extensionVersion: TEST_VERSION,
    storage: memoryStorage(),
    now: () => 1000,
    fetchImpl: async (url, options = {}) => {
      retryCalls.push({ url, options });
      if (url.endsWith("/auth/session")) {
        sessionNumber += 1;
        return jsonResponse(200, {
          ok: true, access_token: `token_${sessionNumber}`, token_type: "DianAgent",
          expires_at: 3601000, expires_in: 3600, install_id: "install_a",
          extension_version: TEST_VERSION,
        });
      }
      apiNumber += 1;
      return apiNumber === 1
        ? jsonResponse(401, { error: "agent_session_expired" })
        : jsonResponse(200, { ok: true });
    },
  });
  const retried = await retryClient.authorizedFetch("/scan-status", { method: "POST", body: "{}" });
  assert.strictEqual(retried.ok, true);
  assert.strictEqual(sessionNumber, 2, "401 must bootstrap exactly one replacement session");
  assert.strictEqual(apiNumber, 2, "the protected request must retry exactly once");
  assert.strictEqual(retryCalls[1].options.headers["X-Dian-Agent-Token"], "token_1");
  assert.strictEqual(retryCalls[3].options.headers["X-Dian-Agent-Token"], "token_2");

  let rejectedSessions = 0;
  let rejectedApiCalls = 0;
  const rejectedClient = auth.createClient({
    baseUrl: "http://127.0.0.1:8765",
    extensionId: "trusted_extension",
    extensionVersion: TEST_VERSION,
    storage: memoryStorage(),
    now: () => 1000,
    fetchImpl: async (url) => {
      if (url.endsWith("/auth/session")) {
        rejectedSessions += 1;
        return jsonResponse(200, {
          ok: true, access_token: `rejected_${rejectedSessions}`, token_type: "DianAgent",
          expires_at: 3601000, expires_in: 3600, install_id: "install_a",
          extension_version: TEST_VERSION,
        });
      }
      rejectedApiCalls += 1;
      return jsonResponse(401, { error: "agent_session_invalid" });
    },
  });
  const rejected = await rejectedClient.authorizedFetch("/stores");
  assert.strictEqual(rejected.status, 401);
  assert.strictEqual(rejectedSessions, 2);
  assert.strictEqual(rejectedApiCalls, 2, "a second 401 must fail closed without a retry loop");

  assert.deepStrictEqual(auth.extensionSourcePayload({
    manifest: { version: "4.13.4" },
    extensionId: "trusted_extension",
    userAgent: "Mozilla/5.0 Chrome/140.0 Safari/537.36",
  }), {
    source: "unpacked", browser: "chrome", version: "4.13.4", extension_id: "trusted_extension",
  });
  assert.deepStrictEqual(auth.extensionSourcePayload({
    manifest: { version: "4.13.4", update_url: "https://edge.microsoft.com/extensionwebstorebase/v1/crx" },
    extensionId: "trusted_extension",
    userAgent: "Mozilla/5.0 Edg/140.0",
  }), {
    source: "edge_addons", browser: "edge", version: "4.13.4", extension_id: "trusted_extension",
  });

  const background = fs.readFileSync(require.resolve("./background.js"), "utf8");
  assert.match(background, /importScripts\("bridge-auth\.js"/);
  assert.match(background, /DianBridgeAuth\.createClient/);
  assert.match(background, /extensionVersion:\s*String\(chrome\.runtime\.getManifest\(\)\.version/);
  assert.strictEqual(
    (background.match(/\bfetch\s*\(/g) || []).length,
    2,
    "only the public liveness and activation probes may bypass the authenticated client",
  );
  assert.match(background, /fetch\(`\$\{BRIDGE_URL\}\/health\/live`/, "the public /health/live probe must stay unauthenticated");
  assert.match(background, /fetch\(`\$\{BRIDGE_URL\}\/activation\/status`/, "the public activation contract must be read before pairing");
  assert.match(background, /"X-Dian-Agent-Extension-Version": String\(runtimeVersion \|\| ""\)/);
  assert.match(background, /reportExtensionInstallSource\("worker-start"\)/);
  assert.match(background, /reportExtensionInstallSource\(`on-installed:\$\{details\.reason\}`\)/);
  assert.match(background, /reportExtensionInstallSource\("browser-startup"\)/);
  assert.match(background, /bridgeFetch\("\/distribution\/extension-source"/);
  assert.match(
    background,
    /const protectedProbe = await withTimeout\([\s\S]{0,700}fetchWithTimeout\(`\$\{BRIDGE_URL\}\/auth\/status`,\s*\{ cache: "no-store" \},\s*2500\)[\s\S]{0,220}3000,[\s\S]{0,100}agent_auth_timeout/,
    "the lightweight authenticated receipt must have an explicit three-second upper bound",
  );
  assert.doesNotMatch(
    background.slice(background.indexOf("async function checkBridge(options")),
    /fetchWithTimeout\(`\$\{BRIDGE_URL\}\/system\/status`/,
    "aggregate system diagnostics must not be used as the authentication heartbeat",
  );
  assert.match(background, /authenticationStatus\.authenticated !== true/);
  assert.match(background, /authenticationStatus\.client_kind !== "browser_extension"/);
  assert.match(background, /authenticatedSubject !== String\(chrome\.runtime\.id/);
  assert.match(background, /authenticatedExtensionVersion !== manifestVersion/);
  assert.match(background, /liveness_ok:\s*true,[\s\S]*authenticated:\s*false/);
  assert.match(background, /VERSION_RELOAD_GUARD_KEY/);
  assert.match(background, /if \(manifest\.update_url\)/);
  assert.ok(
    background.indexOf("if (comparison < 0)") < background.indexOf("if (manifest.update_url)"),
    "a store-managed extension newer than the Agent must report agent_version_older first",
  );
  assert.match(background, /chrome\.runtime\.reload\(\)/);

  const livenessStart = background.indexOf("async function updateStatusBestEffort");
  const livenessEnd = background.indexOf("\nasync function checkBridge(options", livenessStart);
  assert.ok(livenessStart >= 0 && livenessEnd > livenessStart, "liveness helper block should exist");
  async function runLiveness(fetchImpl) {
    let updateAttempts = 0;
    const sandbox = {
      AbortController,
      BRIDGE_URL: "http://127.0.0.1:8765",
      BRIDGE_LIVENESS_TIMEOUT_MS: 3000,
      fetch: fetchImpl,
      async updateStatus() {
        updateAttempts += 1;
        throw new Error("chrome.storage.local unavailable");
      },
      setTimeout() { return 1; },
      clearTimeout() {},
    };
    vm.runInNewContext(background.slice(livenessStart, livenessEnd), sandbox, {
      filename: "background-liveness-truth.vm.js",
    });
    const result = await sandbox.checkBridgeLiveness();
    return { result, updateAttempts };
  }

  const healthyTruth = await runLiveness(async () => ({
    ok: true,
    status: 200,
    async json() { return { version: "4.13.4" }; },
  }));
  assert.strictEqual(healthyTruth.result.ok, true, "status-cache failure must not turn a healthy /health/live response offline");
  assert.strictEqual(healthyTruth.result.data.version, "4.13.4");
  assert.strictEqual(healthyTruth.updateAttempts, 1);

  const offlineTruth = await runLiveness(async () => {
    throw new Error("connect ECONNREFUSED 127.0.0.1:8765");
  });
  assert.strictEqual(offlineTruth.result.ok, false, "offline liveness must remain a fulfilled negative verdict when status caching fails");
  assert.match(offlineTruth.result.error, /ECONNREFUSED/);
  assert.strictEqual(offlineTruth.updateAttempts, 1);

  console.log("background bridge auth tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
