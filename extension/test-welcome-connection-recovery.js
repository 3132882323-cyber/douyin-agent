const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(path.join(__dirname, "welcome.js"), "utf8");

function fakeNode(id) {
  const listeners = new Map();
  return {
    id,
    hidden: false,
    disabled: false,
    textContent: "",
    className: "",
    addEventListener(type, listener) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(listener);
    },
    dispatch(type) {
      return Promise.all((listeners.get(type) || []).map((listener) => listener({ type, target: this })));
    },
  };
}

function recoverableSessionError(code = "agent_session_invalid") {
  const error = new Error(code === "agent_session_expired" ? "The local Agent session has expired." : "The local Agent session is invalid.");
  error.status = 401;
  error.code = code;
  error.payload = { error: code };
  return error;
}

function contextInvalidatedError() {
  return new Error("Extension context invalidated.");
}

function connectionError({ message, code = "", status = 0 }) {
  const error = new Error(message);
  error.code = code;
  error.status = status;
  return error;
}

function readyStoreCatalog(storeCount = 1) {
  const stores = Array.from({ length: Math.max(1, storeCount) }, (_, index) => ({
    key: `store-ready-${String(index + 1).padStart(4, "0")}`,
    page_count: index === 0 ? 4 : 1,
    doudian_page_count: index === 0 ? 4 : 1,
    doudian_fresh: true,
  }));
  return {
    store_count: storeCount,
    selected_store_key: stores[0].key,
    stores,
  };
}

function createHarness({
  fetchStores,
  fetchHealth = null,
  fetchActivation = null,
  runtimeVersion = "4.14.0",
  runtimeContextInvalidated = false,
  runtimeUpdateUrl = "",
  runtimeMessage = null,
  openDoudianTabs = [{ id: 201, url: "https://fxg.jinritemai.com/ffa/mshop/homepage/index" }],
  sessionSeed = [],
  now = Date.now(),
}) {
  const nodes = new Map();
  const documentListeners = new Map();
  const windowListeners = new Map();
  const timerQueue = [];
  const sessionValues = new Map(sessionSeed);
  let currentTime = Number(now);
  let nextTimerId = 1;
  const location = {
    hash: "",
    reloadCalls: 0,
    reload() { this.reloadCalls += 1; },
  };
  const client = {
    clearCalls: 0,
    fetchCalls: 0,
    activeFetches: 0,
    maxActiveFetches: 0,
    async clearSession() {
      this.clearCalls += 1;
    },
    async fetchJson(pathname) {
      assert.equal(pathname, "/stores");
      this.fetchCalls += 1;
      this.activeFetches += 1;
      this.maxActiveFetches = Math.max(this.maxActiveFetches, this.activeFetches);
      try {
        return await fetchStores({ call: this.fetchCalls });
      } finally {
        this.activeFetches -= 1;
      }
    },
  };
  const document = {
    visibilityState: "visible",
    hidden: false,
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, fakeNode(id));
      return nodes.get(id);
    },
    addEventListener(type, listener) {
      if (!documentListeners.has(type)) documentListeners.set(type, []);
      documentListeners.get(type).push(listener);
    },
  };
  const window = {
    addEventListener(type, listener) {
      if (!windowListeners.has(type)) windowListeners.set(type, []);
      windowListeners.get(type).push(listener);
    },
  };
  const sandbox = {
    console,
    AbortController,
    Date: { now: () => currentTime },
    document,
    window,
    location,
    sessionStorage: {
      getItem(key) { return sessionValues.has(key) ? sessionValues.get(key) : null; },
      setItem(key, value) { sessionValues.set(key, String(value)); },
      removeItem(key) { sessionValues.delete(key); },
    },
    navigator: { platform: "Win32", userAgent: "Chrome" },
    chrome: {
      runtime: {
        getURL: (value) => `chrome-extension://test/${value}`,
        getManifest: () => {
          if (runtimeContextInvalidated) throw contextInvalidatedError();
          return { version: runtimeVersion, update_url: runtimeUpdateUrl };
        },
        sendMessage: async (message) => {
          if (message?.type === "test-bridge" && runtimeMessage) return runtimeMessage(message);
          return { ok: true };
        },
      },
      tabs: {
        create() {},
        async query(query = {}) {
          return query.url === "https://fxg.jinritemai.com/*" ? openDoudianTabs : [];
        },
      },
    },
    DianBridgeAuth: { createClient: () => client },
    fetch: async (url, options = {}) => {
      if (/\/activation\/status$/.test(String(url))) {
        if (fetchActivation) return fetchActivation({ url, options });
        return {
          ok: true,
          status: 200,
          async json() {
            return {
              activation_contract_version: 1,
              agent_version: "4.14.0",
              required_extension_version: "4.14.0",
              installed_extension_version: "4.14.0",
              activation: { state: "active", ready: true, reload_required: false },
            };
          },
        };
      }
      assert.match(String(url), /\/health$/);
      if (fetchHealth) return fetchHealth({ url, options });
      return {
        ok: true,
        status: 200,
        async json() { return { status: "ok", version: "4.14.0", platform: "windows" }; },
      };
    },
    setTimeout(callback, delay = 0) {
      const normalizedDelay = Number(delay || 0);
      const timer = {
        id: nextTimerId++, callback, delay: normalizedDelay,
        dueAt: currentTime + normalizedDelay, cancelled: false,
      };
      timerQueue.push(timer);
      return timer.id;
    },
    clearTimeout(id) {
      const timer = timerQueue.find((candidate) => candidate.id === id);
      if (timer) timer.cancelled = true;
    },
  };
  sandbox.globalThis = sandbox;
  vm.runInNewContext(source, sandbox, { filename: "welcome-connection-recovery.vm.js" });

  async function emit(target, type) {
    const listeners = target === "window" ? windowListeners.get(type) : documentListeners.get(type);
    await Promise.all((listeners || []).map((listener) => listener({ type, target: target === "window" ? window : document })));
    await Promise.resolve();
    await Promise.resolve();
  }

  async function runPendingTimers() {
    for (const timer of timerQueue.splice(0)) {
      if (!timer.cancelled) {
        currentTime = Math.max(currentTime, timer.dueAt);
        await timer.callback();
      }
    }
    await Promise.resolve();
  }

  async function runTimersWithDelay(delay) {
    const selected = timerQueue.filter((timer) => timer.delay === delay);
    for (const timer of selected) {
      const index = timerQueue.indexOf(timer);
      if (index >= 0) timerQueue.splice(index, 1);
      if (!timer.cancelled) {
        currentTime = Math.max(currentTime, timer.dueAt);
        await timer.callback();
      }
    }
    await Promise.resolve();
  }

  function pendingTimerDelays() {
    return timerQueue.filter((timer) => !timer.cancelled).map((timer) => timer.delay);
  }

  return {
    sandbox, client, nodes, document, location, sessionValues, emit,
    runPendingTimers, runTimersWithDelay, pendingTimerDelays,
    now: () => currentTime,
  };
}

async function waitFor(predicate, message) {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (predicate()) return;
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.fail(message);
}

(async () => {
  {
    const harness = createHarness({
      async fetchStores() { return readyStoreCatalog(2); },
    });
    await harness.sandbox.checkBridge();
    assert.equal(harness.client.clearCalls, 0, "welcome must leave session recovery to DianBridgeAuth");
    assert.equal(harness.client.fetchCalls, 1, "welcome must issue one logical stores request");
    assert.equal(harness.nodes.get("store-status").textContent, "抖店已连接，可以开始巡店");
    assert.equal(harness.nodes.get("store-status").className, "state ok");
    assert.equal(harness.nodes.get("check-bridge").disabled, false);
    assert.match(
      harness.nodes.get("activation-version-layers").textContent,
      /Agent v4\.14\.0 · 磁盘扩展 v4\.14\.0 · 浏览器运行 v4\.14\.0/,
    );
  }

  for (const scenario of [
    {
      label: "an unopened Doudian tab must not be presented as connected",
      openDoudianTabs: [],
      catalog: readyStoreCatalog(1),
      expected: "尚未打开抖店，请先登录并保持页面打开",
    },
    {
      label: "an open Doudian tab without discovered stores still needs a scan",
      catalog: { store_count: 0, selected_store_key: "", stores: [] },
      expected: "抖店已打开，尚未读取经营数据",
    },
    {
      label: "a discovered but unconfirmed store must remain a warning",
      catalog: { store_count: 1, selected_store_key: "", stores: [{ key: "store-unconfirmed", page_count: 2, doudian_page_count: 2, doudian_fresh: true }] },
      expected: "抖店已打开，等待确认当前店铺",
    },
    {
      label: "a selected store without snapshots still needs its first scan",
      catalog: { store_count: 1, selected_store_key: "store-empty", stores: [{ key: "store-empty", page_count: 0, doudian_page_count: 0, doudian_fresh: false }] },
      expected: "店铺已识别，等待首次巡店",
    },
    {
      label: "Qianchuan-only history must not make Doudian look ready",
      catalog: { store_count: 1, selected_store_key: "store-ads-only", stores: [{ key: "store-ads-only", page_count: 3, doudian_page_count: 0, doudian_fresh: false }] },
      expected: "店铺已识别，等待首次巡店",
    },
    {
      label: "stale Doudian history must request a refresh instead of showing green",
      catalog: { store_count: 1, selected_store_key: "store-stale", stores: [{ key: "store-stale", page_count: 4, doudian_page_count: 4, doudian_fresh: false }] },
      expected: "店铺已识别，经营数据需要刷新",
    },
  ]) {
    const harness = createHarness({
      openDoudianTabs: scenario.openDoudianTabs,
      async fetchStores() { return scenario.catalog; },
    });
    await harness.sandbox.checkBridge();
    assert.equal(harness.nodes.get("store-status").textContent, scenario.expected, scenario.label);
    assert.equal(harness.nodes.get("store-status").className, "state warn", scenario.label);
  }

  {
    let activationHeader = "";
    const harness = createHarness({
      runtimeVersion: "4.13.5",
      async fetchStores() { throw new Error("stores must not run while the runtime is stale"); },
      async runtimeMessage() { throw contextInvalidatedError(); },
      async fetchActivation({ options }) {
        activationHeader = options.headers["X-Dian-Agent-Extension-Version"];
        return {
          ok: true,
          status: 200,
          async json() {
            return {
              activation_contract_version: 1,
              agent_version: "4.14.0",
              required_extension_version: "4.14.0",
              installed_extension_version: "4.14.0",
              activation: { state: "reload_required", ready: false, reload_required: true },
            };
          },
        };
      },
    });
    await harness.sandbox.checkBridge();
    assert.equal(activationHeader, "4.13.5", "activation must receive the runtime version cached at script startup");
    assert.equal(harness.client.fetchCalls, 0, "runtime mismatch must reload before authentication or stores access");
    assert.match(harness.nodes.get("bridge-status").textContent, /扩展正在切换到 v4\.14\.0/);
    assert.match(
      harness.nodes.get("activation-version-layers").textContent,
      /Agent v4\.14\.0 · 磁盘扩展 v4\.14\.0 · 浏览器运行 v4\.13\.5/,
    );
    assert.equal(harness.pendingTimerDelays().filter((delay) => delay === 800).length, 1);
    await harness.runTimersWithDelay(800);
    assert.equal(harness.location.reloadCalls, 1, "a stale runtime must reload the welcome page exactly once");

    const guarded = createHarness({
      runtimeVersion: "4.13.5",
      sessionSeed: [...harness.sessionValues.entries()],
      now: harness.now(),
      async fetchStores() { throw new Error("stores must remain blocked while the runtime is stale"); },
      async runtimeMessage() { throw contextInvalidatedError(); },
      async fetchActivation() {
        return {
          ok: true,
          status: 200,
          async json() {
            return {
              activation_contract_version: 1,
              agent_version: "4.14.0",
              required_extension_version: "4.14.0",
              installed_extension_version: "4.14.0",
              activation: { state: "reload_required", ready: false, reload_required: true },
            };
          },
        };
      },
    });
    await guarded.sandbox.checkBridge();
    await guarded.runTimersWithDelay(0);
    assert.equal(guarded.location.reloadCalls, 0, "the ten-second guard must stop a stale-runtime reload loop");
    assert.match(guarded.nodes.get("store-status").textContent, /自动复检/);
    const guardedRetryDelay = guarded.pendingTimerDelays().find((delay) => delay > 800);
    assert.ok(guardedRetryDelay > 800, "the replacement page must schedule one bounded delayed recovery check");
    await guarded.runTimersWithDelay(guardedRetryDelay);
    await waitFor(
      () => guarded.pendingTimerDelays().includes(800),
      "the delayed recovery check must request a second page load after the background worker had time to reload",
    );
    await guarded.runTimersWithDelay(800);
    assert.equal(guarded.location.reloadCalls, 1, "the delayed background reload race must recover with one bounded second page load");

    const activated = createHarness({
      runtimeVersion: "4.14.0",
      sessionSeed: [...guarded.sessionValues.entries()],
      now: guarded.now(),
      async fetchStores() { return readyStoreCatalog(2); },
    });
    await activated.sandbox.checkBridge();
    assert.equal(activated.nodes.get("store-status").textContent, "抖店已连接，可以开始巡店");
    assert.equal(activated.location.reloadCalls, 0);
    assert.equal(activated.sessionValues.has("dianAgentWelcomeContextReloadV1"), false, "successful activation must clear the recovery guard");
  }

  {
    const harness = createHarness({
      runtimeVersion: "4.14.0",
      async fetchStores() { throw new Error("stores must remain blocked for a broken disk installation"); },
      async runtimeMessage() { throw new Error("background reload must not run for a broken disk installation"); },
      async fetchActivation() {
        return {
          ok: true,
          status: 200,
          async json() {
            return {
              activation_contract_version: 1,
              agent_version: "4.14.0",
              required_extension_version: "4.14.0",
              installed_extension_version: "4.13.6",
              activation: { state: "installed_version_mismatch", ready: false, reload_required: false },
            };
          },
        };
      },
    });
    await harness.sandbox.checkBridge();
    assert.equal(harness.client.fetchCalls, 0, "a broken disk installation must fail closed before stores access");
    assert.equal(harness.location.reloadCalls, 0);
    assert.equal(harness.pendingTimerDelays().includes(800), false, "disk mismatch requires repair, not a page reload loop");
    assert.match(harness.nodes.get("store-status").textContent, /Repair Dian Agent/);
  }

  for (const scenario of [
    {
      runtimeVersion: "4.15.0",
      runtimeUpdateUrl: "",
      expected: /运行新版安装包更新 Agent/,
      label: "a newer unpacked runtime must not try to downgrade itself by reloading the page",
    },
    {
      runtimeVersion: "4.13.6",
      runtimeUpdateUrl: "https://clients2.google.com/service/update2/crx",
      expected: /扩展管理页检查商店更新/,
      label: "a store-managed old runtime must wait for the browser store instead of reloading the page",
    },
  ]) {
    const harness = createHarness({
      runtimeVersion: scenario.runtimeVersion,
      runtimeUpdateUrl: scenario.runtimeUpdateUrl,
      async fetchStores() { throw new Error("version direction mismatch must block stores access"); },
      async runtimeMessage() { throw new Error("the welcome page must not ask background to reload this version direction"); },
      async fetchActivation() {
        return {
          ok: true,
          status: 200,
          async json() {
            return {
              activation_contract_version: 1,
              agent_version: "4.14.0",
              required_extension_version: "4.14.0",
              installed_extension_version: "4.14.0",
              activation: { state: "reload_required", ready: false, reload_required: true },
            };
          },
        };
      },
    });
    await harness.sandbox.checkBridge();
    assert.equal(harness.client.fetchCalls, 0);
    assert.equal(harness.location.reloadCalls, 0);
    assert.equal(harness.pendingTimerDelays().includes(800), false, scenario.label);
    assert.match(harness.nodes.get("store-status").textContent, scenario.expected);
    assert.equal(harness.nodes.get("repair-agent-guide").hidden, true, "version upgrades must not offer a repair action that cannot install the newer package");
  }

  {
    let activationHeader = "unexpected";
    const stalePage = createHarness({
      runtimeContextInvalidated: true,
      async fetchStores() { throw new Error("an invalidated startup context must reload before stores access"); },
      async fetchActivation({ options }) {
        activationHeader = options.headers["X-Dian-Agent-Extension-Version"];
        return {
          ok: true,
          status: 200,
          async json() {
            return {
              activation_contract_version: 1,
              agent_version: "4.14.0",
              required_extension_version: "4.14.0",
              installed_extension_version: "4.14.0",
              activation: { state: "installed_unconfirmed", ready: false, reload_required: false },
            };
          },
        };
      },
    });
    await stalePage.sandbox.checkBridge();
    assert.equal(activationHeader, "", "a dead startup context must use the public unconfirmed activation contract");
    assert.equal(stalePage.client.fetchCalls, 0);
    assert.equal(stalePage.pendingTimerDelays().filter((delay) => delay === 800).length, 1);
    await stalePage.runTimersWithDelay(800);
    assert.equal(stalePage.location.reloadCalls, 1, "an invalidated startup document must receive one bounded page reload");

    const activated = createHarness({
      runtimeVersion: "4.14.0",
      sessionSeed: [...stalePage.sessionValues.entries()],
      now: stalePage.now(),
      async fetchStores() { return readyStoreCatalog(1); },
    });
    await activated.sandbox.checkBridge();
    assert.equal(activated.nodes.get("store-status").textContent, "抖店已连接，可以开始巡店");
    assert.equal(activated.sessionValues.has("dianAgentWelcomeContextReloadV1"), false);
  }

  {
    const harness = createHarness({
      runtimeVersion: "4.14.0",
      async fetchStores() { throw new Error("an old Agent must be upgraded before stores access"); },
      async fetchHealth() {
        return {
          ok: true,
          status: 200,
          async json() { return { status: "ok", version: "4.13.6", platform: "windows" }; },
        };
      },
      async fetchActivation() {
        return { ok: false, status: 401, async json() { return { error: "agent_session_required" }; } };
      },
    });
    await harness.sandbox.checkBridge();
    assert.equal(harness.client.fetchCalls, 0);
    assert.equal(harness.location.reloadCalls, 0, "a new extension paired with an old Agent must never enter a reload loop");
    assert.equal(harness.nodes.get("repair-agent-guide").hidden, false);
    assert.match(harness.nodes.get("bridge-status").textContent, /Agent v4\.13\.6 需要升级/);
    assert.match(harness.nodes.get("store-status").textContent, /v4\.14\.0.*安装包更新本地 Agent/);
    assert.match(harness.nodes.get("repair-agent-instruction").textContent, /v4\.14\.0 Windows 安装包/);
  }

  {
    let healthSignal = null;
    const harness = createHarness({
      async fetchStores() { return readyStoreCatalog(1); },
      async fetchHealth({ options }) {
        healthSignal = options.signal;
        return {
          ok: true,
          status: 200,
          json() { return new Promise(() => {}); },
        };
      },
    });
    const check = harness.sandbox.checkBridge();
    await waitFor(
      () => harness.pendingTimerDelays().includes(3000),
      "the health timeout must remain active while the response body is pending",
    );
    await harness.runTimersWithDelay(3000);
    await check;
    assert.equal(healthSignal?.aborted, true, "a body that never resolves must abort the health request");
    assert.equal(harness.client.fetchCalls, 0, "stores must not be read before the complete health body is validated");
    assert.equal(harness.nodes.get("check-bridge").disabled, false, "the timeout must release the check button");
    assert.equal(
      harness.pendingTimerDelays().filter((delay) => delay === 3000).length,
      1,
      "a timed-out health body may schedule one bounded transient recheck",
    );
  }

  {
    let healthJsonCalls = 0;
    const harness = createHarness({
      async fetchStores() { return readyStoreCatalog(1); },
      async fetchHealth() {
        return {
          ok: false,
          status: 403,
          async json() {
            healthJsonCalls += 1;
            return { error: "forbidden" };
          },
        };
      },
    });
    await assert.rejects(
      () => harness.sandbox.fetchHealth(),
      (error) => error?.code === "welcome_health_http_error" && error?.status === 403,
      "health HTTP failures must preserve their status",
    );
    await harness.sandbox.checkBridge();
    assert.equal(healthJsonCalls, 0, "a non-2xx health response must be rejected before body parsing");
    assert.equal(harness.client.fetchCalls, 0);
    assert.equal(
      harness.pendingTimerDelays().filter((delay) => [3000, 10000, 30000].includes(delay)).length,
      0,
      "health 4xx is a hard error and must not schedule polling",
    );
  }

  {
    const harness = createHarness({
      async fetchStores() { return readyStoreCatalog(1); },
      async fetchHealth() {
        return {
          ok: true,
          status: 200,
          async json() { return { version: "4.14.0" }; },
        };
      },
    });
    await harness.sandbox.checkBridge();
    assert.equal(harness.client.fetchCalls, 0);
    assert.equal(
      harness.pendingTimerDelays().filter((delay) => [3000, 10000, 30000].includes(delay)).length,
      0,
      "an invalid health schema is a hard protocol error and must not schedule polling",
    );
  }

  for (const activation of ["focus", "visibilitychange"]) {
    let repaired = false;
    const harness = createHarness({
      async fetchStores() {
        if (!repaired) throw recoverableSessionError("agent_session_expired");
        return readyStoreCatalog(3);
      },
    });
    await harness.sandbox.checkBridge();
    const callsBeforeRepair = harness.client.fetchCalls;
    repaired = true;
    if (activation === "focus") await harness.emit("window", "focus");
    else {
      harness.document.visibilityState = "visible";
      harness.document.hidden = false;
      await harness.emit("document", "visibilitychange");
    }
    await harness.runPendingTimers();
    await waitFor(
      () => harness.client.fetchCalls > callsBeforeRepair && harness.nodes.get("store-status").textContent === "抖店已连接，可以开始巡店",
      `returning through ${activation} should automatically recheck a repaired Agent`,
    );
  }

  {
    const harness = createHarness({
      async fetchStores() { throw recoverableSessionError(); },
    });
    await harness.sandbox.checkBridge();
    assert.equal(harness.client.clearCalls, 0, "welcome must not add a second recovery loop above DianBridgeAuth");
    assert.equal(harness.client.fetchCalls, 1, "a terminal session error must stop after the bridge client's bounded retry");
    const detail = harness.nodes.get("store-status").textContent;
    assert.equal(detail, "认证会话已失效，自动刷新失败；请重新加载扩展，仍未恢复再运行 Repair Dian Agent");
    assert.equal(harness.nodes.get("store-status").title, "agent_session_invalid", "the exact terminal failure code must remain inspectable");
    assert.doesNotMatch(detail, /本地 Agent 未启动/, "an authenticated-store failure must not be reported as a stopped Agent");
    assert.equal(harness.pendingTimerDelays().filter((delay) => delay === 3000).length, 0, "terminal session errors must not poll forever");
  }

  for (const terminalError of [
    connectionError({ message: "Agent internal error", code: "agent_internal_error", status: 500 }),
    connectionError({ message: "读取店铺连接超时", code: "welcome_check_timeout" }),
    connectionError({ message: "Failed to fetch" }),
  ]) {
    const harness = createHarness({
      async fetchStores() { throw terminalError; },
    });
    await harness.sandbox.checkBridge();
    assert.equal(harness.client.clearCalls, 0, `${terminalError.code || terminalError.message} must not clear a valid session`);
    assert.equal(harness.client.fetchCalls, 1, `${terminalError.code || terminalError.message} must not be treated as stale authentication`);
    assert.match(harness.nodes.get("store-status").textContent, /暂时|超时|自动重新检测/);
    assert.equal(harness.pendingTimerDelays().filter((delay) => delay === 3000).length, 1, "transient failures must have one bounded recheck timer");
  }

  {
    const harness = createHarness({
      async fetchStores() {
        throw connectionError({ message: "Agent internal error", code: "agent_internal_error", status: 500 });
      },
    });
    await harness.sandbox.checkBridge();
    for (const [delay, expectedCalls, nextDelay] of [
      [3000, 2, 10000],
      [10000, 3, 30000],
      [30000, 4, null],
    ]) {
      await harness.runTimersWithDelay(delay);
      await waitFor(
        () => harness.client.fetchCalls === expectedCalls
          && (nextDelay === null || harness.pendingTimerDelays().includes(nextDelay)),
        `transient retry ${delay}ms should settle before the next backoff`,
      );
    }
    assert.equal(harness.client.clearCalls, 0);
    assert.equal(harness.client.fetchCalls, 4, "one check plus three backoff retries is the hard upper bound");
    assert.equal(harness.pendingTimerDelays().filter((delay) => [3000, 10000, 30000].includes(delay)).length, 0);
  }

  for (const terminalError of [
    connectionError({ message: "extension not trusted", code: "extension_origin_not_trusted", status: 403 }),
    connectionError({ message: "install auth corrupt", code: "agent_install_auth_corrupt", status: 500 }),
    connectionError({ message: "repair is required", code: "unexpected_auth_state", status: 500 }),
  ]) {
    terminalError.payload = ["agent_install_auth_corrupt", "unexpected_auth_state"].includes(terminalError.code)
      ? { repair_required: true }
      : {};
    const harness = createHarness({
      async fetchStores() { throw terminalError; },
    });
    await harness.sandbox.checkBridge();
    assert.equal(harness.client.clearCalls, 0);
    assert.equal(harness.client.fetchCalls, 1);
    assert.match(harness.nodes.get("store-status").textContent, /扩展 ID 未授权|认证配置已损坏/);
    assert.equal(harness.pendingTimerDelays().filter((delay) => delay === 3000).length, 0, "operator-action failures must wait for focus, visibility, or a manual click");
  }

  {
    const harness = createHarness({
      async fetchStores() { throw contextInvalidatedError(); },
    });
    await harness.sandbox.checkBridge();
    assert.equal(harness.client.clearCalls, 0, "an invalidated extension context is not an Agent session failure");
    assert.equal(harness.client.fetchCalls, 1, "an invalidated context must not retry through the dead extension API");
    await harness.sandbox.checkBridge();
    assert.equal(harness.client.fetchCalls, 2, "a second explicit check may observe the same dead page without starting another reload");
    assert.equal(harness.pendingTimerDelays().filter((delay) => delay === 0).length, 1, "the in-memory guard must keep one reload callback");
    await harness.runTimersWithDelay(0);
    assert.equal(harness.location.reloadCalls, 1, "the stale welcome tab should reload exactly once");
    assert.equal(harness.nodes.get("store-status").textContent, "扩展已更新，正在重新载入页面");
    assert.ok(harness.sessionValues.has("dianAgentWelcomeContextReloadV1"), "the reload loop guard must be persisted for this tab");
    assert.equal(harness.pendingTimerDelays().filter((delay) => [3000, 10000, 30000].includes(delay)).length, 0);

    const guarded = createHarness({
      sessionSeed: [...harness.sessionValues.entries()],
      now: harness.now(),
      async fetchStores() { throw contextInvalidatedError(); },
    });
    await guarded.sandbox.checkBridge();
    await guarded.runTimersWithDelay(0);
    assert.equal(guarded.location.reloadCalls, 0, "the ten-second session guard must prevent a reload loop in the replacement page");
    assert.equal(guarded.client.clearCalls, 0);
    assert.match(guarded.nodes.get("store-status").textContent, /自动复检/);
    assert.equal(guarded.pendingTimerDelays().filter((delay) => delay > 10000).length, 1, "one delayed recovery check must replace permanent manual intervention");
    assert.equal(guarded.pendingTimerDelays().filter((delay) => [3000, 10000, 30000].includes(delay)).length, 0);
  }

  {
    let releaseFirstFetch;
    const firstFetch = new Promise((resolve) => { releaseFirstFetch = resolve; });
    const harness = createHarness({
      async fetchStores({ call }) {
        if (call === 1) return firstFetch;
        return readyStoreCatalog(1);
      },
    });
    const directChecks = [
      harness.sandbox.checkBridge(),
      harness.sandbox.checkBridge(),
      harness.sandbox.checkBridge(),
    ];
    const lifecycleChecks = [
      harness.emit("window", "focus"),
      harness.emit("window", "focus"),
      harness.emit("document", "visibilitychange"),
    ];
    await waitFor(() => harness.client.fetchCalls === 1, "the shared stores request should start");
    assert.equal(harness.client.fetchCalls, 1, "overlapping direct and lifecycle checks must share one in-flight request");
    assert.equal(harness.client.maxActiveFetches, 1);
    releaseFirstFetch(readyStoreCatalog(1));
    await Promise.all([...directChecks, ...lifecycleChecks]);
    await harness.runPendingTimers();
    await waitFor(() => harness.client.activeFetches === 0, "all coalesced checks should settle");
    assert.equal(harness.client.maxActiveFetches, 1, "automatic rechecks must never overlap stores requests");
    assert.ok(harness.client.fetchCalls <= 2, "a burst may schedule at most one trailing recheck");
  }

  console.log("welcome connection recovery tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
