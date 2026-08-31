"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const auth = require("./bridge-auth.js");
const TEST_VERSION = "4.14.0";

for (const htmlFile of ["sidepanel.html", "popup.html", "welcome.html"]) {
  const html = fs.readFileSync(path.join(__dirname, htmlFile), "utf8");
  assert.match(html, /<script src="bridge-auth\.js"><\/script>[\s\S]*<script src="(?:sidepanel|popup|welcome)\.js"><\/script>/);
}
const build = fs.readFileSync(path.join(__dirname, "..", "build_extension.ps1"), "utf8");
assert.match(build, /"bridge-auth\.js"/);
assert.match(build, /"scan-scope-policy\.js"/);

function response(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return payload; },
    clone() { return response(status, payload); },
  };
}

function memoryStorage(initial = {}) {
  const values = { ...initial };
  return {
    values,
    async get(key) { return { [key]: values[key] }; },
    async set(next) { Object.assign(values, next); },
    async remove(key) { delete values[key]; },
  };
}

(async () => {
  const now = 1_800_000_000_000;
  const storage = memoryStorage();
  const calls = [];
  const client = auth.createClient({
    baseUrl: "http://127.0.0.1:8765",
    extensionId: "trusted-extension",
    extensionVersion: TEST_VERSION,
    storage,
    now: () => now,
    fetchImpl: async (url, options = {}) => {
      calls.push({ url, options });
      if (url.endsWith("/auth/session")) {
        return response(200, {
          ok: true,
          access_token: "token-1",
          token_type: "DianAgent",
          expires_at: Math.floor(now / 1000) + 600,
          expires_in: 600,
          install_id: "install-a",
          extension_version: TEST_VERSION,
        });
      }
      return response(200, { ok: true, stores: [] });
    },
  });

  await client.fetchJson("/stores");
  await client.fetchJson("/health");
  assert.equal(calls.filter((item) => item.url.endsWith("/auth/session")).length, 1);
  assert.equal(calls[0].options.headers["X-Dian-Agent"], "2");
  assert.equal(calls[0].options.headers[auth.EXTENSION_ID_HEADER], "trusted-extension");
  assert.equal(calls[0].options.headers[auth.EXTENSION_ID_HEADER], "trusted-extension");
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    extension_id: "trusted-extension",
    extension_version: TEST_VERSION,
  });
  assert.equal(calls[0].options.headers["X-Dian-Agent-Extension-Version"], TEST_VERSION);
  assert.equal(calls[1].options.headers["X-Dian-Agent-Token"], "token-1");
  assert.equal(calls[1].options.headers[auth.EXTENSION_ID_HEADER], "trusted-extension");
  assert.equal(calls[1].options.headers[auth.EXTENSION_ID_HEADER], "trusted-extension");
  assert.equal(calls[1].options.headers["X-Dian-Agent-Extension-Version"], TEST_VERSION);
  assert.equal(storage.values[auth.STORAGE_KEY].install_id, "install-a");

  const staleStorage = memoryStorage({
    [auth.STORAGE_KEY]: {
      access_token: "old-version-token",
      token_type: "DianAgent",
      expires_at: Math.floor(now / 1000) + 600,
      extension_version: "4.13.9",
    },
  });
  const staleCalls = [];
  const upgradedClient = auth.createClient({
    extensionId: "trusted-extension",
    extensionVersion: TEST_VERSION,
    storage: staleStorage,
    now: () => now,
    fetchImpl: async (url, options = {}) => {
      staleCalls.push({ url, options });
      if (url.endsWith("/auth/session")) return response(200, {
        ok: true,
        access_token: "current-version-token",
        token_type: "DianAgent",
        expires_at: Math.floor(now / 1000) + 600,
        extension_version: TEST_VERSION,
      });
      assert.equal(options.headers["X-Dian-Agent-Token"], "current-version-token");
      return response(200, { ok: true });
    },
  });
  await upgradedClient.fetchJson("/stores");
  assert.equal(
    staleCalls.filter((item) => item.url.endsWith("/auth/session")).length,
    1,
    "a cache from another extension version must never reach a protected route",
  );
  assert.equal(staleStorage.values[auth.STORAGE_KEY].extension_version, TEST_VERSION);

  let authCount = 0;
  let protectedCount = 0;
  const retryStorage = memoryStorage();
  const retryClient = auth.createClient({
    baseUrl: "http://127.0.0.1:8765",
    extensionId: "trusted-extension",
    extensionVersion: TEST_VERSION,
    storage: retryStorage,
    now: () => now,
    fetchImpl: async (url, options = {}) => {
      if (url.endsWith("/auth/session")) {
        authCount += 1;
        return response(200, {
          ok: true,
          access_token: `token-${authCount}`,
          token_type: "DianAgent",
          expires_at: Math.floor(now / 1000) + 600,
          extension_version: TEST_VERSION,
        });
      }
      protectedCount += 1;
      if (protectedCount === 1) return response(401, { error: "agent_session_expired" });
      assert.equal(options.headers["X-Dian-Agent-Token"], "token-2");
      return response(200, { ok: true });
    },
  });
  assert.deepEqual(await retryClient.fetchJson("/stores"), { ok: true });
  assert.equal(authCount, 2, "recognized 401 must refresh exactly once");
  assert.equal(protectedCount, 2, "recognized 401 must retry the request exactly once");

  // Chromium may omit Origin on extension GET requests.  Reproduce the real
  // page-sync -> workbench sequence against a tiny subject-bound Agent model:
  // the successful POST and the following GET must both carry the explicit
  // extension identity, and a cached token from another extension may recover
  // at most once without either client losing its own authenticated session.
  const extensionA = "a".repeat(32);
  const extensionB = "b".repeat(32);
  const extensionC = "c".repeat(32);
  const trustedExtensions = new Set([extensionA, extensionB]);
  const sharedStorage = memoryStorage();
  const subjectByToken = new Map();
  const flowCalls = [];
  let issuedSessionCount = 0;
  let crossClientRejectCount = 0;
  const originlessAgent = async (url, options = {}) => {
    const headers = options.headers || {};
    const claimedExtension = String(headers[auth.EXTENSION_ID_HEADER] || "");
    flowCalls.push({ url, method: options.method || "GET", headers: { ...headers } });
    assert.equal(headers.Origin, undefined, "the regression model must not rely on Chromium sending Origin");
    if (url.endsWith("/auth/session")) {
      const requested = JSON.parse(options.body || "{}").extension_id;
      if (!trustedExtensions.has(claimedExtension) || requested !== claimedExtension) {
        return response(403, {
          error: "extension_pairing_not_authorized",
          message: "该扩展 ID 尚未预授权",
        });
      }
      issuedSessionCount += 1;
      const accessToken = `session-${claimedExtension}-${issuedSessionCount}`;
      subjectByToken.set(accessToken, claimedExtension);
      return response(200, {
        ok: true,
        access_token: accessToken,
        token_type: "DianAgent",
        expires_at: Math.floor(now / 1000) + 600,
        expires_in: 600,
        install_id: "install-flow",
        subject: claimedExtension,
        extension_version: TEST_VERSION,
      });
    }
    const tokenSubject = subjectByToken.get(String(headers["X-Dian-Agent-Token"] || ""));
    if (!trustedExtensions.has(claimedExtension) || tokenSubject !== claimedExtension) {
      crossClientRejectCount += 1;
      return response(401, {
        error: "agent_session_invalid",
        message: "The local Agent session belongs to another client.",
      });
    }
    if (url.endsWith("/push")) return response(200, { ok: true, stored: true });
    if (url.endsWith("/auth/status")) {
      return response(200, {
        authentication_contract_version: 1,
        authenticated: true,
        client_kind: "browser_extension",
        session_subject: claimedExtension,
        session_extension_version: TEST_VERSION,
        required_extension_version: TEST_VERSION,
      });
    }
    return response(404, { error: "not_found" });
  };
  const pageSyncClient = auth.createClient({
    extensionId: extensionA,
    extensionVersion: TEST_VERSION,
    storage: sharedStorage,
    now: () => now,
    fetchImpl: originlessAgent,
  });
  assert.deepEqual(
    await pageSyncClient.fetchJson("/push", { method: "POST", body: "{}" }),
    { ok: true, stored: true },
    "the page sync must finish through the current extension session",
  );
  const workbenchReceipt = await pageSyncClient.fetchJson("/auth/status");
  assert.equal(workbenchReceipt.authenticated, true);
  assert.equal(workbenchReceipt.session_subject, extensionA);
  assert.equal(crossClientRejectCount, 0, "sync followed by workbench auth must not split into another client");

  const duplicateClient = auth.createClient({
    extensionId: extensionB,
    extensionVersion: TEST_VERSION,
    storage: sharedStorage,
    now: () => now,
    fetchImpl: originlessAgent,
  });
  const duplicateReceipt = await duplicateClient.fetchJson("/auth/status");
  assert.equal(duplicateReceipt.authenticated, true);
  assert.equal(duplicateReceipt.session_subject, extensionB);
  assert.ok(crossClientRejectCount <= 1, "a stale cross-client cache may be rejected only once before re-pairing");
  assert.equal(
    (await pageSyncClient.fetchJson("/auth/status")).session_subject,
    extensionA,
    "a second trusted extension session must not invalidate the already authenticated current extension",
  );

  const protectedCallsBeforeUntrusted = flowCalls.filter((call) => !call.url.endsWith("/auth/session")).length;
  const obsoleteClient = auth.createClient({
    extensionId: extensionC,
    extensionVersion: TEST_VERSION,
    storage: memoryStorage(),
    now: () => now,
    fetchImpl: originlessAgent,
  });
  await assert.rejects(
    () => obsoleteClient.fetchJson("/auth/status"),
    (error) => error.status === 403 && error.code === "extension_pairing_not_authorized",
  );
  assert.equal(
    flowCalls.filter((call) => !call.url.endsWith("/auth/session")).length,
    protectedCallsBeforeUntrusted,
    "an obsolete/untrusted extension must stop at pairing and never reach protected business routes",
  );

  let refusedCalls = 0;
  const refusedClient = auth.createClient({
    extensionId: "trusted-extension",
    extensionVersion: TEST_VERSION,
    storage: memoryStorage(),
    now: () => now,
    fetchImpl: async (url) => {
      if (url.endsWith("/auth/session")) return response(200, {
        ok: true,
        access_token: "token",
        token_type: "DianAgent",
        expires_at: Math.floor(now / 1000) + 600,
        extension_version: TEST_VERSION,
      });
      refusedCalls += 1;
      return response(401, { error: "some_other_error" });
    },
  });
  await assert.rejects(() => refusedClient.fetchJson("/stores"), (error) => error.status === 401);
  assert.equal(refusedCalls, 1, "unrecognized 401 must not be retried");

  let repeatedAuth = 0;
  let repeatedProtected = 0;
  const repeatedClient = auth.createClient({
    extensionId: "trusted-extension",
    extensionVersion: TEST_VERSION,
    storage: memoryStorage(),
    now: () => now,
    fetchImpl: async (url) => {
      if (url.endsWith("/auth/session")) {
        repeatedAuth += 1;
        return response(200, {
          ok: true,
          access_token: `repeated-${repeatedAuth}`,
          token_type: "DianAgent",
          expires_at: Math.floor(now / 1000) + 600,
          extension_version: TEST_VERSION,
        });
      }
      repeatedProtected += 1;
      return response(401, { error: "agent_session_invalid" });
    },
  });
  await assert.rejects(() => repeatedClient.fetchJson("/stores"), (error) => error.status === 401);
  assert.equal(repeatedAuth, 2);
  assert.equal(repeatedProtected, 2, "a repeated recognized 401 must stop after the single retry");

  let timeoutAuthCount = 0;
  let timeoutProtectedCount = 0;
  let hangingSignal = null;
  const timeoutClient = auth.createClient({
    extensionId: "trusted-extension",
    extensionVersion: TEST_VERSION,
    storage: memoryStorage(),
    now: () => now,
    sessionTimeoutMs: 250,
    fetchImpl: async (url, options = {}) => {
      if (url.endsWith("/auth/session")) {
        timeoutAuthCount += 1;
        if (timeoutAuthCount === 1) {
          hangingSignal = options.signal;
          return new Promise(() => {});
        }
        return response(200, {
          ok: true,
          access_token: "recovered-token",
          token_type: "DianAgent",
          expires_at: Math.floor(now / 1000) + 600,
          extension_version: TEST_VERSION,
        });
      }
      timeoutProtectedCount += 1;
      assert.equal(options.headers["X-Dian-Agent-Token"], "recovered-token");
      return response(200, { ok: true, recovered: true });
    },
  });
  const timedOutCalls = await Promise.allSettled([
    timeoutClient.fetchJson("/stores"),
    timeoutClient.fetchJson("/health"),
  ]);
  for (const result of timedOutCalls) {
    assert.equal(result.status, "rejected");
    assert.equal(result.reason?.code, "agent_session_timeout");
  }
  assert.equal(timeoutAuthCount, 1, "concurrent callers must share the timed-out session promise");
  assert.equal(hangingSignal?.aborted, true, "session timeout must abort the hanging fetch signal");
  assert.deepEqual(await timeoutClient.fetchJson("/stores"), { ok: true, recovered: true });
  assert.equal(timeoutAuthCount, 2, "a timed-out session promise must be cleared so the next call can retry");
  assert.equal(timeoutProtectedCount, 1, "only the recovered call may reach the protected endpoint");

  assert.equal(auth.isUsableSession({ access_token: "x", token_type: "DianAgent", expires_at: now / 1000 + 60 }, now), true);
  assert.equal(auth.isUsableSession({ access_token: "x", token_type: "DianAgent", expires_at: now / 1000 + 60, extension_version: "4.13.9" }, now, 0, TEST_VERSION), false);
  assert.equal(auth.isRefreshableAuthFailure(401, { error: "agent_session_invalid" }), true);
  assert.equal(auth.isRefreshableAuthFailure(403, { error: "agent_session_invalid" }), false);
  assert.equal(auth.EXTENSION_ID_HEADER, "X-Dian-Agent-Extension-Id");
  console.log("bridge auth tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
