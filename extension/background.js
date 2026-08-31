/** 店策 Agent - MV3 service worker (v4.14.9) */

importScripts("bridge-auth.js", "scan-scope-policy.js", "scan-policy.js", "execution-readback-policy.js", "execution-sender-policy.js");

const BRIDGE_URL = "http://127.0.0.1:8765";
const BRIDGE_LIVENESS_TIMEOUT_MS = 3000;
const QIANCHUAN_ENTRY_URL = "https://qianchuan.jinritemai.com/";
const ALARM_NAME = "dian-agent-sync";
const FULL_SCAN_ALARM = "dian-agent-full-scan";
const BRIDGE_HEALTH_ALARM = "dian-agent-bridge-health";
const VERSION_RECONCILE_ALARM = "dian-agent-extension-version-reconcile";
const VERSION_RELOAD_VERIFY_ALARM = "dian-agent-extension-reload-verify";
const SCAN_RECOVERY_ALARM = "dian-agent-scan-recovery";
const EXECUTION_READBACK_ALARM = "dian-agent-execution-readback";
const EXECUTION_READBACK_STORAGE_KEY = "executionReadbackJobsV1";
const VERSION_RELOAD_GUARD_KEY = "dianAgentVersionReloadV1";
const VERSION_RELOAD_SESSION_KEY = "dianAgentVersionReloadSessionV1";
const VERSION_RELOAD_MAX_ATTEMPTS = 2;
const VERSION_RELOAD_COOLDOWN_MS = 45 * 1000;
const VERSION_RECONCILE_PERIOD_MINUTES = 5;
const WORKBENCH_PATH = "sidepanel.html";
const ACTIVATION_DOCUMENT_REFRESH_KEY = "dianAgentActivationDocumentsV1";
const PLATFORM_ASSISTANT_DISMISSED_KEY = "platformAssistantDismissedV2";
const PLATFORM_ASSISTANT_LEGACY_DISMISSED_KEY = "platformAssistantDismissedV1";
const PAGE_SYNC_RECEIPTS_KEY = "pageSyncReceiptsV1";
const TRUSTED_PLATFORM_MESSAGE_TYPES = new Set([
  "page-data",
  "resolve-execution-identity",
  "platform-assistant-status",
  "platform-assistant-sync",
  "platform-assistant-open-workbench",
]);
const DEFAULT_SETTINGS = {
  autoSync: false,
  intervalMinutes: 5,
  autoFullScan: false,
  fullScanIntervalHours: 6,
  privacyMode: true,
  operatingMode: "sentinel",
};
const bridgeClient = globalThis.DianBridgeAuth.createClient({
  baseUrl: BRIDGE_URL,
  extensionId: chrome.runtime.id || "",
  extensionVersion: String(chrome.runtime.getManifest().version || ""),
});
let versionReconcileInFlight = null;
// Fail closed until storage.session has been checked.  A fresh MV3 worker can
// receive a privileged message before its first bridge reconciliation.
let versionReloadTransitionPending = true;
let versionReloadStateHydrationPromise = null;
let activationDocumentRefreshInFlight = null;
let activeReloadTrackedOperations = 0;
let scanRecoveryPromise = null;

function bridgePath(urlOrPath = "") {
  const value = String(urlOrPath || "");
  if (value.startsWith(BRIDGE_URL)) return value.slice(BRIDGE_URL.length) || "/";
  if (value.startsWith("/")) return value;
  throw new Error("拒绝向本地 Agent 之外的地址附加会话凭证");
}

function bridgeFetch(urlOrPath, options = {}) {
  return bridgeClient.authorizedFetch(bridgePath(urlOrPath), options);
}

async function responseJson(response) {
  try { return await response.json(); } catch (_) { return {}; }
}

async function reportExtensionInstallSource(trigger = "worker") {
  const manifest = chrome.runtime.getManifest();
  const source = DianBridgeAuth.extensionSourcePayload({
    manifest,
    extensionId: chrome.runtime.id || "",
    userAgent: globalThis.navigator?.userAgent || "",
  });
  const response = await bridgeFetch("/distribution/extension-source", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
    body: JSON.stringify(source),
  });
  const payload = await responseJson(response);
  if (!response.ok || payload.ok !== true || payload.extension?.origin_verified !== true) {
    const error = new Error(payload.message || payload.error || `HTTP ${response.status}`);
    error.code = String(payload.error || payload.error_code || "extension_source_report_failed");
    error.status = Number(response.status || 0);
    throw error;
  }
  await chrome.storage.local.set({
    extensionSourceReport: {
      trigger,
      version: source.version,
      extension_id: source.extension_id,
      reported_at: payload.extension.reported_at || new Date().toISOString(),
    },
  });
  return payload.extension;
}

function parsedExtensionVersion(value = "") {
  const version = String(value || "").trim();
  if (!/^\d+(?:\.\d+){2,3}$/.test(version)) return null;
  const parts = version.split(".").map((part) => Number(part));
  if (parts.some((part) => !Number.isSafeInteger(part) || part < 0 || part > 65535)) return null;
  while (parts.length < 4) parts.push(0);
  return { version, parts };
}

function compareExtensionVersions(left = "", right = "") {
  const leftVersion = parsedExtensionVersion(left);
  const rightVersion = parsedExtensionVersion(right);
  if (!leftVersion || !rightVersion) return null;
  for (let index = 0; index < 4; index += 1) {
    if (leftVersion.parts[index] !== rightVersion.parts[index]) {
      return leftVersion.parts[index] > rightVersion.parts[index] ? 1 : -1;
    }
  }
  return 0;
}

async function clearVersionReloadGuards() {
  const cleanup = [
    chrome.storage.local.remove(VERSION_RELOAD_GUARD_KEY).catch(() => undefined),
    chrome.alarms.clear(VERSION_RELOAD_VERIFY_ALARM).catch(() => undefined),
  ];
  if (typeof chrome.storage.session?.remove === "function") {
    cleanup.push(chrome.storage.session.remove(VERSION_RELOAD_SESSION_KEY).catch(() => undefined));
  }
  await Promise.all(cleanup);
}

function createVersionReloadVerificationAlarm() {
  if (typeof chrome.alarms?.create !== "function") {
    throw new Error("浏览器不支持扩展版本复核闹钟，已停止自动重载");
  }
  chrome.alarms.create(VERSION_RELOAD_VERIFY_ALARM, { delayInMinutes: 1 });
}

function activeExtensionReloadWorkReasons() {
  const reasons = [];
  if (fullScanPromise) reasons.push("active_full_scan");
  if (scanRecoveryPromise) reasons.push("scan_recovery");
  if (activeReloadTrackedOperations > 0) reasons.push("active_snapshot_sync");
  if (authorizedExecutionPromises.size) reasons.push("authorized_execution");
  if (typeof manualExecutionReadbackPromises !== "undefined" && manualExecutionReadbackPromises.size) {
    reasons.push("manual_execution_readback");
  }
  if (executionJournalAdvancePromises.size) reasons.push("execution_journal_advance");
  if (executionReadbackRunPromises.size) reasons.push("execution_readback_run");
  if (executionReadbackRecoveryPromise) reasons.push("execution_readback_recovery");
  return reasons;
}

async function extensionReloadBusyReasons() {
  const reasons = activeExtensionReloadWorkReasons();
  try {
    const stored = await chrome.storage.local.get("fullScan");
    if (stored.fullScan?.status === "running") reasons.push("persisted_full_scan");
  } catch {
    reasons.push("persistent_scan_state_unavailable");
  }
  return [...new Set(reasons)];
}

async function drainStorageMutationQueueForVersionReload() {
  while (true) {
    const pending = storageMutationQueue;
    await Promise.resolve(pending).catch(() => undefined);
    if (pending === storageMutationQueue) return;
  }
}

function reloadDeferredBusyResult(base, attempts, reasons) {
  createVersionReloadVerificationAlarm();
  return {
    ...base,
    version_match: false,
    reload_state: "reload_deferred_busy",
    reload_deferred: true,
    reload_attempts: attempts,
    reload_busy_reasons: reasons,
  };
}

async function ensureVersionReconcileAlarm() {
  if (typeof chrome.alarms?.get !== "function" || typeof chrome.alarms?.create !== "function") return false;
  const existing = await chrome.alarms.get(VERSION_RECONCILE_ALARM).catch(() => null);
  if (!existing) {
    chrome.alarms.create(VERSION_RECONCILE_ALARM, {
      delayInMinutes: 1,
      periodInMinutes: VERSION_RECONCILE_PERIOD_MINUTES,
    });
  }
  return true;
}

function ensureVersionReloadStateHydrated() {
  if (versionReloadStateHydrationPromise) return versionReloadStateHydrationPromise;
  versionReloadTransitionPending = true;
  versionReloadStateHydrationPromise = (async () => {
    if (typeof chrome.storage.local?.get !== "function" || typeof chrome.storage.session?.get !== "function") {
      return { hydrated: false, reload_pending: true, reason: "session_guard_unavailable" };
    }
    try {
      const [localStored, sessionStored] = await Promise.all([
        chrome.storage.local.get(VERSION_RELOAD_GUARD_KEY),
        chrome.storage.session.get(VERSION_RELOAD_SESSION_KEY),
      ]);
      const guard = localStored[VERSION_RELOAD_GUARD_KEY] || {};
      const session = sessionStored[VERSION_RELOAD_SESSION_KEY] || {};
      const requestedAt = Number(session.requested_at || 0);
      const localRequestedAt = Number(guard.last_requested_at || 0);
      const sessionPending = Boolean(
        String(session.pair || "").trim()
        && requestedAt > 0
        && Date.now() - requestedAt < VERSION_RELOAD_COOLDOWN_MS
      );
      const localPending = Boolean(
        String(guard.pair || "").trim()
        && localRequestedAt > 0
      );
      const reloadPending = sessionPending || localPending;
      versionReloadTransitionPending = reloadPending;
      return { hydrated: true, reload_pending: reloadPending, local_pending: localPending, session_pending: sessionPending };
    } catch (error) {
      // An unreadable durable guard is an unknown transition state.  Keep the
      // admission barrier closed until a later worker/check can prove safety.
      versionReloadTransitionPending = true;
      return {
        hydrated: false,
        reload_pending: true,
        reason: "session_guard_unavailable",
        error: String(error?.message || error || ""),
      };
    }
  })();
  return versionReloadStateHydrationPromise;
}

async function restoreVersionReloadGuardState(localGuard = {}, sessionGuard = {}) {
  const restore = [];
  if (localGuard && Object.keys(localGuard).length) {
    restore.push(chrome.storage.local.set({ [VERSION_RELOAD_GUARD_KEY]: localGuard }));
  } else {
    restore.push(chrome.storage.local.remove(VERSION_RELOAD_GUARD_KEY));
  }
  if (sessionGuard && Object.keys(sessionGuard).length) {
    restore.push(chrome.storage.session.set({ [VERSION_RELOAD_SESSION_KEY]: sessionGuard }));
  } else {
    restore.push(chrome.storage.session.remove(VERSION_RELOAD_SESSION_KEY));
  }
  await Promise.all(restore);
}

// Start hydration before listeners can process a later event-loop turn.
ensureVersionReloadStateHydrated();

async function reconcileRequiredExtensionVersion(activationStatus = {}, trigger = "bridge-check") {
  if (versionReconcileInFlight) return versionReconcileInFlight;
  versionReconcileInFlight = (async () => {
    await ensureVersionReloadStateHydrated();
    const manifest = chrome.runtime.getManifest();
    const runtimeVersion = String(manifest.version || "").trim();
    const required = String(activationStatus.required_extension_version || "").trim();
    const installed = String(activationStatus.installed_extension_version || "").trim();
    const activation = activationStatus.activation && typeof activationStatus.activation === "object"
      ? activationStatus.activation
      : {};
    const comparison = compareExtensionVersions(required, runtimeVersion);
    const base = {
      required_extension_version: required,
      installed_extension_version: installed,
      runtime_extension_version: runtimeVersion,
      trigger: String(trigger || "bridge-check"),
      reload_requested: false,
    };

    if (comparison === 0 && installed === required && activation.ready === true) {
      versionReloadTransitionPending = false;
      await clearVersionReloadGuards();
      return { ...base, version_match: true, reload_state: "matched", reload_attempts: 0 };
    }
    if (comparison === null) {
      return { ...base, version_match: false, reload_state: "invalid_version_contract", reload_blocked: true, reload_attempts: 0 };
    }
    if (!parsedExtensionVersion(installed)) {
      return { ...base, version_match: false, reload_state: "installation_repair_required", reload_blocked: true, reload_attempts: 0 };
    }
    if (installed !== required) {
      return { ...base, version_match: false, reload_state: "installation_repair_required", reload_blocked: true, reload_attempts: 0 };
    }
    if (comparison === 0 || activation.reload_required !== true) {
      return { ...base, version_match: false, reload_state: "activation_contract_conflict", reload_blocked: true, reload_attempts: 0 };
    }
    if (comparison < 0) {
      return { ...base, version_match: false, reload_state: "agent_version_older", reload_blocked: true, reload_attempts: 0 };
    }
    if (manifest.update_url) {
      return { ...base, version_match: false, reload_state: "browser_store_update_required", reload_blocked: true, reload_attempts: 0 };
    }
    if (!chrome.storage.session?.get || !chrome.storage.session?.set) {
      return { ...base, version_match: false, reload_state: "session_guard_unavailable", reload_blocked: true, reload_attempts: 0 };
    }

    const pair = `${required}|${runtimeVersion}`;
    const now = Date.now();
    const [localStored, sessionStored] = await Promise.all([
      chrome.storage.local.get(VERSION_RELOAD_GUARD_KEY),
      chrome.storage.session.get(VERSION_RELOAD_SESSION_KEY),
    ]);
    const saved = localStored[VERSION_RELOAD_GUARD_KEY] || {};
    const session = sessionStored[VERSION_RELOAD_SESSION_KEY] || {};
    const sameSavedPair = saved.pair === pair;
    const attempts = sameSavedPair ? Math.max(0, Number(saved.attempts || 0)) : 0;
    const lastRequestedAt = sameSavedPair ? Math.max(0, Number(saved.last_requested_at || 0)) : 0;
    const sessionReloadPending = session.pair === pair
      && Number(session.requested_at || 0) > 0
      && now - Number(session.requested_at) < VERSION_RELOAD_COOLDOWN_MS;
    if (sessionReloadPending) versionReloadTransitionPending = true;
    const immediateBusyReasons = await extensionReloadBusyReasons();
    if (immediateBusyReasons.length) {
      return reloadDeferredBusyResult(base, attempts, immediateBusyReasons);
    }
    if (sessionReloadPending) {
      return { ...base, version_match: false, reload_state: "reload_pending", reload_attempts: attempts };
    }
    if (attempts >= VERSION_RELOAD_MAX_ATTEMPTS) {
      return { ...base, version_match: false, reload_state: "reload_blocked", reload_blocked: true, reload_attempts: attempts };
    }
    if (lastRequestedAt && now - lastRequestedAt < VERSION_RELOAD_COOLDOWN_MS) {
      return { ...base, version_match: false, reload_state: "reload_cooldown", reload_attempts: attempts };
    }

    versionReloadTransitionPending = true;
    let reloadRequested = false;
    let reloadGuardWriteStarted = false;
    let reloadGuardRestored = false;
    try {
      let busyReasons = await extensionReloadBusyReasons();
      if (busyReasons.length) return reloadDeferredBusyResult(base, attempts, busyReasons);

      await drainStorageMutationQueueForVersionReload();
      busyReasons = await extensionReloadBusyReasons();
      if (busyReasons.length) return reloadDeferredBusyResult(base, attempts, busyReasons);

      const nextAttempts = attempts + 1;
      const guard = {
        schema_version: 2,
        pair,
        required_extension_version: required,
        runtime_extension_version: runtimeVersion,
        attempts: nextAttempts,
        first_requested_at: sameSavedPair ? Number(saved.first_requested_at || now) : now,
        last_requested_at: now,
        last_trigger: String(trigger || "bridge-check"),
      };
      reloadGuardWriteStarted = true;
      await chrome.storage.local.set({ [VERSION_RELOAD_GUARD_KEY]: guard });
      await chrome.storage.session.set({
        [VERSION_RELOAD_SESSION_KEY]: {
          pair,
          requested_at: now,
          attempt: nextAttempts,
        },
      });
      await drainStorageMutationQueueForVersionReload();
      // The transition latch rejects every new tracked workflow while the
      // guard writes await.  Recheck once more after those awaits so a missed
      // or already-running path can never be reloaded underneath.
      busyReasons = await extensionReloadBusyReasons();
      if (busyReasons.length) {
        await restoreVersionReloadGuardState(saved, session);
        reloadGuardRestored = true;
        return reloadDeferredBusyResult(base, attempts, busyReasons);
      }
      createVersionReloadVerificationAlarm();
      chrome.runtime.reload();
      reloadRequested = true;
      return {
        ...base,
        version_match: false,
        reload_state: "reload_requested",
        reload_requested: true,
        reload_attempts: nextAttempts,
      };
    } finally {
      if (!reloadRequested && (!reloadGuardWriteStarted || reloadGuardRestored)) {
        versionReloadTransitionPending = false;
      }
    }
  })();
  try {
    return await versionReconcileInFlight;
  } finally {
    versionReconcileInFlight = null;
  }
}

const FULL_SCAN_PAGES = [
  { id: "overview", label: "经营概览", source: "doudian", url: "https://fxg.jinritemai.com/ffa/mshop/homepage/index", waitMs: 5500 },
  { id: "orders", label: "订单管理", source: "doudian", url: "https://fxg.jinritemai.com/ffa/morder/order/list", waitMs: 6000, harvestList: true },
  { id: "products", label: "商品管理", source: "doudian", url: "https://fxg.jinritemai.com/ffa/g/list", waitMs: 6000, harvestList: true },
  { id: "inventory", label: "库存管理", source: "doudian", url: "https://fxg.jinritemai.com/ffa/g/stock-manage/list", waitMs: 6000, harvestList: true },
  { id: "refunds", label: "售后工作台", source: "doudian", url: "https://fxg.jinritemai.com/ffa/merchant-aftersale-workbench/aftersale/list", waitMs: 6000, harvestList: true },
  { id: "reviews", label: "评价管理", source: "doudian", url: "https://fxg.jinritemai.com/ffa/maftersale/comment", waitMs: 6000, harvestList: true },
  { id: "shelf", label: "商城运营", source: "doudian", url: "https://fxg.jinritemai.com/ffa/growth-common/growth-shelf", waitMs: 7000 },
  { id: "live", label: "直播管理", source: "doudian", url: "https://fxg.jinritemai.com/ffa/content-tool/shop-live", waitMs: 8000 },
  { id: "short_video", label: "短视频运营", source: "doudian", url: "https://fxg.jinritemai.com/ffa/content-tool/short-video", waitMs: 5000, harvestList: true, collectTimeoutMs: 24000 },
  { id: "image_text", label: "图文运营", source: "doudian", url: "https://fxg.jinritemai.com/ffa/content-tool/image-text-operation", waitMs: 4000, collectTimeoutMs: 9000 },
  { id: "search", label: "搜索运营", source: "doudian", url: "https://fxg.jinritemai.com/ffa/mcompass/search", waitMs: 6500, harvestList: true },
  { id: "recommend_card", label: "推荐卡运营", source: "doudian", url: "https://fxg.jinritemai.com/ffa/recommend-card/home", waitMs: 6500, harvestList: true },
  { id: "funds", label: "账户中心", source: "doudian", url: "https://fxg.jinritemai.com/ffa/fund-control/account-center", waitMs: 5500 },
  { id: "qianchuan_video_library", expectedPageTypes: ["video_library", "materials"], label: "千川视频库与素材分析", source: "qianchuan", url: "https://qianchuan.jinritemai.com/dataV2/roi2-material-analysis", waitMs: 5000, harvestList: true, collectTimeoutMs: 24000 },
  { id: "qianchuan_overview", expectedPageType: "overview", label: "千川经营首页", source: "qianchuan", url: QIANCHUAN_ENTRY_URL, fallbackUrls: ["https://qianchuan.jinritemai.com/home"], waitMs: 4500 },
  { id: "qianchuan_campaigns", expectedPageType: "campaigns", label: "千川商品推广", source: "qianchuan", url: "https://qianchuan.jinritemai.com/uni-prom", tabTexts: ["商品全域推广", "商品推广"], waitMs: 5500, harvestList: true, collectTimeoutMs: 24000 },
  { id: "qianchuan_live", expectedPageType: "qianchuan_live", label: "千川直播推广", source: "qianchuan", url: "https://qianchuan.jinritemai.com/uni-prom", tabTexts: ["直播全域推广", "直播推广", "直播间推广"], waitMs: 5500, harvestList: true, collectTimeoutMs: 24000 },
  { id: "qianchuan_live_dashboard", expectedPageType: "live_dashboard", label: "千川直播大屏", source: "qianchuan", url: "https://qianchuan.jinritemai.com/board-next", waitMs: 5500 },
];

const scanPageRegistry = DianAgentScanScopePolicy.validatePageRegistry(FULL_SCAN_PAGES);
if (!scanPageRegistry.ok) {
  throw new Error(`巡店页面注册表与范围契约不一致：${JSON.stringify(scanPageRegistry)}`);
}

const SOURCE_PATTERNS = {
  doudian: ["https://fxg.jinritemai.com/*"],
  qianchuan: ["https://qianchuan.jinritemai.com/*", "https://buyin.jinritemai.com/*"],
};

const SOURCE_URLS = {
  doudian: "https://fxg.jinritemai.com/ffa/mshop/homepage/index",
  qianchuan: QIANCHUAN_ENTRY_URL,
};

function sourceForPlatformUrl(url = "") {
  const value = String(url || "");
  if (value.startsWith("https://fxg.jinritemai.com/")) return "doudian";
  if (value.startsWith("https://qianchuan.jinritemai.com/") || value.startsWith("https://buyin.jinritemai.com/")) return "qianchuan";
  return "";
}

async function ensurePlatformAssistantForTab(tabId, url = "") {
  if (!Number.isInteger(Number(tabId)) || !sourceForPlatformUrl(url)) return false;
  try {
    await chrome.scripting.executeScript({
      target: { tabId: Number(tabId) },
      files: ["platform-assistant.js"],
    });
    const presence = await chrome.scripting.executeScript({
      target: { tabId: Number(tabId) },
      func: async (attributeName) => {
        await new Promise((resolve) => setTimeout(resolve, 100));
        return Boolean(document.querySelector(`[${attributeName}="true"]`));
      },
      args: ["data-dian-agent-platform-assistant"],
    });
    return presence.some((item) => item?.result === true);
  } catch (_) {
    // Login redirects, discarded tabs and pages that close during startup are
    // expected. The manifest will inject the assistant on the next full load.
    return false;
  }
}

async function ensureOpenPlatformAssistants() {
  const groups = await Promise.all([
    querySourceTabs("doudian").catch(() => []),
    querySourceTabs("qianchuan").catch(() => []),
  ]);
  const tabs = [...groups[0], ...groups[1]]
    .filter((tab, index, values) => Number.isInteger(tab?.id) && values.findIndex((item) => item?.id === tab.id) === index);
  const results = await Promise.all(tabs.map((tab) => ensurePlatformAssistantForTab(tab.id, tab.url)));
  return results.filter(Boolean).length;
}

async function refreshStaleExtensionDocumentsOnce(trigger = "worker-start") {
  if (activationDocumentRefreshInFlight) return activationDocumentRefreshInFlight;
  activationDocumentRefreshInFlight = (async () => {
    const runtimeVersion = String(chrome.runtime.getManifest()?.version || "");
    const stored = await chrome.storage.local.get(ACTIVATION_DOCUMENT_REFRESH_KEY);
    const previous = stored[ACTIVATION_DOCUMENT_REFRESH_KEY] || {};
    if (previous.runtime_version === runtimeVersion && previous.extension_id === chrome.runtime.id) {
      return { refreshed: 0, already_current: true };
    }
    // Commit the guard before reloading tabs. Reloading an extension document
    // can wake the worker again; the persisted version prevents a loop.
    await chrome.storage.local.set({
      [ACTIVATION_DOCUMENT_REFRESH_KEY]: {
        runtime_version: runtimeVersion,
        extension_id: chrome.runtime.id,
        refreshed_at: Date.now(),
        trigger: String(trigger || "worker-start"),
      },
    });
    const extensionBase = chrome.runtime.getURL("");
    const refreshablePaths = new Set(["/welcome.html", "/sidepanel.html", "/upgrade.html", "/sync.html"]);
    const tabs = await chrome.tabs.query({});
    const targets = tabs.filter((tab) => {
      if (!Number.isInteger(tab?.id) || !String(tab.url || "").startsWith(extensionBase)) return false;
      try { return refreshablePaths.has(new URL(tab.url).pathname); } catch (_) { return false; }
    });
    await Promise.all(targets.map((tab) => chrome.tabs.reload(tab.id).catch(() => undefined)));
    return { refreshed: targets.length, already_current: false };
  })();
  try {
    return await activationDocumentRefreshInFlight;
  } finally {
    activationDocumentRefreshInFlight = null;
  }
}

async function coordinateActivationDocuments(trigger = "worker-start") {
  const [documents, assistants] = await Promise.all([
    refreshStaleExtensionDocumentsOnce(trigger),
    ensureOpenPlatformAssistants(),
  ]);
  return { documents, assistants };
}

// Serialize read-modify-write operations so concurrent tabs cannot overwrite
// each other's catalog or status entries.
let storageMutationQueue = Promise.resolve();
let fullScanPromise = null;
let activeFullScanRunId = "";
let activeFullScanTabId = null;
const cancelledFullScanRuns = new Set();
const doudianScanContexts = new Map();

function clearDoudianScanContext(tabId) {
  if (Number.isInteger(tabId)) doudianScanContexts.delete(tabId);
}

function snapshotHasIdentityConflict(snapshot, source = "") {
  const explicitConflict = Boolean(
    snapshot
    && typeof snapshot === "object"
    && (
      String(snapshot.identity_status || "") === "conflict"
      || (Array.isArray(snapshot.identity_conflicts) && snapshot.identity_conflicts.length > 0)
    )
  );
  if (explicitConflict) return true;
  if (
    source === "doudian"
    && typeof globalThis.DianAgentScanPolicy?.inspectDoudianCurrentIdentityProof === "function"
  ) {
    return globalThis.DianAgentScanPolicy.inspectDoudianCurrentIdentityProof(snapshot)?.code === "STORE_IDENTITY_CONFLICT";
  }
  return false;
}

function requireDoudianSnapshotIdentity(snapshot, options = {}) {
  const proof = DianAgentScanPolicy.inspectDoudianCurrentIdentityProof(snapshot);
  if (proof.ok) return proof;
  const conflict = proof.code === "STORE_IDENTITY_CONFLICT" || snapshotHasIdentityConflict(snapshot, "doudian");
  // Conflicting candidates are unsafe for current data, but the local Agent
  // still needs the original evidence to persist a forensic quarantine row.
  if (conflict && options.allowConflictForForensics === true) return proof;
  const error = new Error(conflict
    ? "检测到店铺正在切换，本次数据没有使用。请回到要经营的抖店首页后重试。"
    : "这个页面暂时不能用于经营判断，请打开抖店首页并刷新后重试。");
  error.code = conflict ? "STORE_IDENTITY_CONFLICT" : "STORE_IDENTITY_UNRESOLVED";
  throw error;
}

async function establishDoudianScanContext(tabId, storeKey, runId) {
  clearDoudianScanContext(tabId);
  const overview = FULL_SCAN_PAGES.find((page) => page.id === "overview" && page.source === "doudian");
  if (!overview) throw new Error("巡店入口暂时不可用，已停止采集。");
  const bootstrapPage = {
    ...overview,
    // A replacement tab must prove identity again. Including the owned tab id
    // prevents the bridge's idempotent page receipt from replaying the proof
    // obtained for a previously closed tab.
    id: `identity_bootstrap_${tabId}`,
    label: "准备当前抖店",
    expectedPageType: "overview",
    requiresIdentityProof: true,
  };
  const verified = await scanOnePage(tabId, bootstrapPage, "identity-bootstrap", {}, storeKey, runId);
  if (!verified?.ok) {
    const error = new Error(verified?.error || "当前抖店页面暂时不能用于经营判断，巡店已停止。");
    error.code = verified?.error_code || "STORE_IDENTITY_UNRESOLVED";
    throw error;
  }
  const context = DianAgentScanPolicy.createDoudianScanContext(storeKey, {
    runId,
    tabId,
    // Fixed 15-minute ceiling covers the normal 13-page route including list
    // pagination. The lease is never renewed while pages are processed.
    ttlMs: 900000,
    proof: {
      verified: true,
      store_key: verified.store_key,
      page_type: verified.page_type,
      identity_source: verified.store_identity_source,
      verified_at: verified.captured_at,
    },
  });
  if (!context) {
    const error = new Error("当前抖店页面已经失效，巡店已停止，请重新打开抖店首页。");
    error.code = "STORE_IDENTITY_UNRESOLVED";
    throw error;
  }
  doudianScanContexts.set(tabId, context);
  return context;
}

function prepareScanSessionSnapshot(source, snapshot, sender) {
  if (source === "doudian") {
    requireDoudianSnapshotIdentity(snapshot, { allowConflictForForensics: true });
  }
  const tabId = sender?.tab?.id;
  if (!Number.isInteger(tabId)) return snapshot;
  const context = doudianScanContexts.get(tabId);
  if (!context) return snapshot;
  const prepared = DianAgentScanPolicy.applyDoudianScanContext(snapshot, context, {
    source,
    tabUrl: sender?.tab?.url || sender?.url || "",
    runId: context.run_id,
    tabId,
  });
  if (prepared.reason === "expired") clearDoudianScanContext(tabId);
  if (prepared.reason !== "fresh_identity_claim" && prepared.reason !== "identity_conflict") {
    const error = new Error("本次页面范围已经失效，数据没有使用；请回到抖店首页后重试。");
    error.code = prepared.reason === "identity_conflict" ? "STORE_IDENTITY_CONFLICT" : "STORE_IDENTITY_UNRESOLVED";
    throw error;
  }
  return prepared.snapshot;
}

function mutateLocalStorage(keys, mutator) {
  const operation = storageMutationQueue
    .catch(() => undefined)
    .then(async () => {
      const current = await chrome.storage.local.get(keys);
      const updates = (await mutator(current)) || {};
      if (Object.keys(updates).length) await chrome.storage.local.set(updates);
      return updates;
    });
  storageMutationQueue = operation.catch(() => undefined);
  return operation;
}

chrome.runtime.onInstalled.addListener(async (details) => {
  const stored = await chrome.storage.local.get("settings");
  await chrome.storage.local.set({
    settings: {
      ...DEFAULT_SETTINGS,
      ...(stored.settings || {}),
      autoSync: false,
      autoFullScan: false,
      operatingMode: "sentinel",
    },
  });
  await configureAlarm();
  await reportExtensionInstallSource(`on-installed:${details.reason}`).catch((error) =>
    updateStatus("bridge", `本地 Agent 已运行，扩展报到失败：${error.message || error}`, "warning"));
  await coordinateActivationDocuments(`on-installed:${details.reason}`).catch(() => undefined);
  await recoverExecutionReadbackJobs("extension-installed").catch(() => undefined);
  await updateStatus("system", "扩展已就绪");
  // Open welcome page on first install
  if (details.reason === "install") {
    chrome.tabs.create({ url: chrome.runtime.getURL("welcome.html") });
  }
});

chrome.runtime.onStartup.addListener(() => {
  configureAlarm().catch(() => undefined);
  reportExtensionInstallSource("browser-startup").catch(() => undefined);
  coordinateActivationDocuments("browser-startup").catch(() => undefined);
  recoverInterruptedScan("browser-startup").catch(() => undefined);
  recoverExecutionReadbackJobs("browser-startup").catch(() => undefined);
});

// MV3 service workers can start without onInstalled/onStartup (for example
// after an unpacked-extension Reload). Report immediately so installers can
// verify that the browser is actually running this exact extension version.
reportExtensionInstallSource("worker-start").catch(() => undefined);
ensureVersionReconcileAlarm().catch(() => undefined);
Promise.resolve()
  .then(() => coordinateActivationDocuments("worker-start"))
  .then(() => recoverExecutionReadbackJobs("worker-start"))
  .catch(() => undefined);

async function rememberRecentQianchuanTab(tabId) {
  if (!Number.isInteger(tabId)) return;
  try {
    const tab = await chrome.tabs.get(tabId);
    if (!DianAgentScanPolicy.isQianchuanUrl(tab?.url)) return;
    await chrome.storage.local.set({
      recentQianchuanTab: {
        tab_id: tab.id,
        window_id: tab.windowId,
        url: tab.url || "",
        title: tab.title || "",
        last_seen: Date.now(),
      },
    });
  } catch {
    // The tab may have closed between activation and lookup.
  }
}

chrome.tabs.onActivated.addListener(({ tabId }) => {
  rememberRecentQianchuanTab(tabId).catch(() => undefined);
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete" && sourceForPlatformUrl(tab?.url)) {
    ensurePlatformAssistantForTab(tabId, tab.url).catch(() => undefined);
  }
  if (tab?.active && (changeInfo.url || changeInfo.status === "complete")) {
    rememberRecentQianchuanTab(tabId).catch(() => undefined);
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  clearDoudianScanContext(tabId);
});

function workbenchUrlForRoute(route = "") {
  const normalized = route === "chengfang-demo" ? route : "";
  return chrome.runtime.getURL(`${WORKBENCH_PATH}${normalized ? `#${normalized}` : ""}`);
}

async function openWorkbench(route = "") {
  const workbenchUrl = chrome.runtime.getURL(WORKBENCH_PATH);
  const targetUrl = workbenchUrlForRoute(route);
  const existingTabs = await chrome.tabs.query({ url: `${workbenchUrl}*` });
  const existing = existingTabs.find((tab) => Number.isInteger(tab.id));
  if (existing?.id) {
    if (Number.isInteger(existing.windowId)) {
      await chrome.windows.update(existing.windowId, { focused: true }).catch(() => undefined);
    }
    await chrome.tabs.update(existing.id, route ? { active: true, url: targetUrl } : { active: true });
    return existing;
  }
  return chrome.tabs.create({ url: targetUrl, active: true });
}

chrome.action.onClicked.addListener(() => {
  openWorkbench().catch((error) => updateStatus("system", `工作台打开失败：${error.message || error}`, "error"));
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === BRIDGE_HEALTH_ALARM) checkBridge({ trigger: "bridge-health-alarm" }).catch(() => undefined);
  if (alarm.name === VERSION_RECONCILE_ALARM) checkBridge({ trigger: "version-reconcile-alarm" }).catch(() => undefined);
  if (alarm.name === VERSION_RELOAD_VERIFY_ALARM) checkBridge({ trigger: "version-reload-verify-alarm" }).catch(() => undefined);
  if (alarm.name === SCAN_RECOVERY_ALARM) recoverInterruptedScan("recovery-alarm").catch(() => undefined);
  if (alarm.name === EXECUTION_READBACK_ALARM) recoverExecutionReadbackJobs("recovery-alarm").catch(() => undefined);
});

async function getSettings() {
  const stored = await chrome.storage.local.get("settings");
  return { ...DEFAULT_SETTINGS, ...(stored.settings || {}) };
}

async function configureAlarm() {
  const settings = await getSettings();
  await chrome.alarms.clear(ALARM_NAME);
  await chrome.alarms.clear(FULL_SCAN_ALARM);
  await chrome.alarms.clear(BRIDGE_HEALTH_ALARM);
  await chrome.alarms.clear(VERSION_RECONCILE_ALARM);
  chrome.alarms.create(VERSION_RECONCILE_ALARM, {
    delayInMinutes: 1,
    periodInMinutes: VERSION_RECONCILE_PERIOD_MINUTES,
  });
  // 轻量哨兵不轮询业务数据。所有模式只低频读取公开、脱敏的扩展
  // 激活合同；只有用户显式切换到完整后台模式时才增加常规健康检查。
  if (settings.operatingMode === "full") {
    chrome.alarms.create(BRIDGE_HEALTH_ALARM, {
      delayInMinutes: 1,
      periodInMinutes: 30,
    });
  }
  // 版本复核只读取本地 Agent 的脱敏运行合同，不会采集任何店铺页面。
  // 页面采集和全店巡检只能由用户动作发起。保留旧设置字段用于兼容
  // 已安装版本，但绝不为它们创建后台采集闹钟。
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

const {
  matchAccount,
  errorCode: scanErrorCode,
  isNonRetryable: isNonRetryableScanError,
  shouldStopAfterResult,
  resumePageIds,
  mergeScopedResults,
  rankSeedTabs,
  buildRecoveryCheckpoint,
  canCancelScan,
  acceptScanStatusTransition,
} = globalThis.DianAgentScanPolicy;

function withTimeout(promise, timeoutMs, message, code = "") {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      const error = new Error(message);
      if (code) error.code = code;
      reject(error);
    }, timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 5000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await bridgeFetch(url, { ...options, signal: controller.signal });
  } catch (error) {
    if (error?.name === "AbortError") throw new Error(`本地 Agent 响应超过 ${Math.round(timeoutMs / 1000)} 秒`);
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function createScanRunId() {
  const suffix = globalThis.crypto?.randomUUID?.().replace(/-/g, "") || Math.random().toString(36).slice(2);
  return `scan_${Date.now().toString(36)}_${suffix.slice(0, 20)}`.toLowerCase();
}

function createExecutionReadbackToken() {
  if (typeof globalThis.crypto?.getRandomValues !== "function") {
    throw new Error("当前浏览器缺少安全随机数能力，不能创建执行回读令牌");
  }
  const bytes = new Uint8Array(32);
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function isScanCancelled(runId) {
  return Boolean(runId && cancelledFullScanRuns.has(runId));
}

const AUTHORITATIVE_SCAN_STATES = new Set([
  "idle", "running", "completed", "partial", "cancelled", "interrupted", "error",
]);

function authoritativeScanCheckpoint(payload = {}) {
  const candidate = payload?.scan && typeof payload.scan === "object" ? payload.scan : payload;
  if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) return null;
  if (!AUTHORITATIVE_SCAN_STATES.has(String(candidate.status || ""))) return null;
  if (!Number.isInteger(candidate.revision) || candidate.revision < 0) return null;
  if (typeof candidate.run_id !== "string") return null;
  return { ...candidate };
}

function scanCheckpointIdentity(scan = {}) {
  return `${String(scan.run_id || "")}:${Number(scan.revision || 0)}`;
}

async function reconcileLocalScanCheckpoint(expected = {}, authority = null, reason = "") {
  const expectedIdentity = scanCheckpointIdentity(expected);
  let reconciled = false;
  await mutateLocalStorage(["fullScan"], ({ fullScan }) => {
    const current = fullScan || {};
    // Never let a late POST/GET receipt overwrite a newer local run.
    if (scanCheckpointIdentity(current) !== expectedIdentity) return {};
    reconciled = true;
    if (authority) {
      return {
        fullScan: {
          ...authority,
          reconciliation_required: false,
          reconciliation_reason: "",
          reconciled_from: "agent",
          reconciled_at: Date.now(),
        },
      };
    }
    return {
      fullScan: {
        ...current,
        reconciliation_required: true,
        reconciliation_reason: String(reason || "scan_checkpoint_ambiguous").slice(0, 120),
        reconciliation_marked_at: Date.now(),
      },
    };
  });
  return reconciled;
}

async function readAuthoritativeScanCheckpoint() {
  const response = await fetchWithTimeout(`${BRIDGE_URL}/scan-status`, { cache: "no-store" }, 4000);
  if (!response.ok) throw new Error(`Agent 巡店状态读取失败：HTTP ${response.status}`);
  const checkpoint = authoritativeScanCheckpoint(await response.json());
  if (!checkpoint) throw new Error("Agent 返回的巡店状态合同无效");
  return checkpoint;
}

async function setFullScanState(patch, runId = "", { replaceRun = false } = {}) {
  let accepted = false;
  const updates = await mutateLocalStorage(["fullScan"], ({ fullScan }) => {
    const current = fullScan || {};
    if (runId && current.run_id && current.run_id !== runId && !replaceRun) return {};
    const sameRun = !runId || !current.run_id || current.run_id === runId;
    if (
      sameRun
      && patch.status
      && !acceptScanStatusTransition(current.status, patch.status, {
        cancelRequested: isScanCancelled(runId),
      })
    ) return {};
    const next = { ...current, ...patch };
    if (runId) next.run_id = runId;
    next.revision = Math.max(Number(current.revision || 0), Number(patch.revision || 0)) + 1;
    if (next.status === "running") next.heartbeat_at = Date.now();
    accepted = true;
    return { fullScan: next };
  });
  if (updates.fullScan) {
    const attemptedCheckpoint = updates.fullScan;
    try {
      const response = await fetchWithTimeout(`${BRIDGE_URL}/scan-status`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
        body: JSON.stringify(attemptedCheckpoint),
      }, 4000);
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        const rejection = String(body.error || body.error_code || `HTTP ${response.status}`).slice(0, 120);
        try {
          const authority = await readAuthoritativeScanCheckpoint();
          await reconcileLocalScanCheckpoint(attemptedCheckpoint, authority);
          await updateStatus(
            "scan_checkpoint",
            `Agent 明确拒绝巡店检查点（${rejection}），本地已按 Agent 权威状态重新对齐。`,
            "warning",
          ).catch(() => undefined);
        } catch (reconcileError) {
          await reconcileLocalScanCheckpoint(attemptedCheckpoint, null, `agent_rejected:${rejection}`);
          await updateStatus(
            "scan_checkpoint",
            `Agent 明确拒绝巡店检查点且权威状态暂不可读：${reconcileError.message || reconcileError}`,
            "warning",
          ).catch(() => undefined);
        }
        return false;
      }
      const authority = authoritativeScanCheckpoint(await response.json().catch(() => null));
      if (!authority) {
        await reconcileLocalScanCheckpoint(attemptedCheckpoint, null, "invalid_agent_accept_receipt");
        await updateStatus("scan_checkpoint", "Agent 接收回执无法验证，已标记等待状态对账。", "warning").catch(() => undefined);
        return false;
      }
      await reconcileLocalScanCheckpoint(attemptedCheckpoint, authority);
    } catch (error) {
      // A timeout or connection reset is ambiguous: the Agent may have
      // committed the POST even though its response was lost. Preserve the
      // local checkpoint and mark it for a later authority read; never roll it
      // back speculatively.
      await reconcileLocalScanCheckpoint(attemptedCheckpoint, null, "network_result_ambiguous");
      await updateStatus("scan_checkpoint", `巡店检查点结果待对账：${error.message || error}`, "warning").catch(() => undefined);
    }
  }
  return accepted;
}

async function selectAnalysisAccount(accountKey = "") {
  await bridgeFetch(`${BRIDGE_URL}/settings`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
    body: JSON.stringify({ qianchuan_account_key: accountKey }),
  }).catch(() => undefined);
}

function waitForTabReady(tabId, expectedUrl = "", timeoutMs = 30000, allowImmediate = true) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const expected = expectedUrl ? new URL(expectedUrl) : null;
    const matchesTarget = (url) => {
      if (!expected) return true;
      try {
        const actual = new URL(url || "about:blank");
        return actual.hostname === expected.hostname && actual.pathname === expected.pathname;
      } catch {
        return false;
      }
    };
    const finish = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      chrome.tabs.onRemoved.removeListener(removedListener);
      error ? reject(error) : resolve();
    };
    const listener = (updatedId, changeInfo, tab) => {
      if (updatedId === tabId && changeInfo.status === "complete" && matchesTarget(tab?.url)) finish();
    };
    const removedListener = (removedId) => {
      if (removedId === tabId) {
        const error = new Error("巡店页面已关闭");
        error.code = "TAB_CLOSED";
        finish(error);
      }
    };
    const timer = setTimeout(() => {
      const error = new Error("页面加载超时");
      error.code = "PAGE_LOAD_TIMEOUT";
      finish(error);
    }, timeoutMs);
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.onRemoved.addListener(removedListener);
    if (allowImmediate) {
      chrome.tabs.get(tabId)
        .then((tab) => {
          if (tab?.status === "complete" && matchesTarget(tab.url)) finish();
        })
        .catch((error) => finish(error));
    }
  });
}

async function navigateScanTab(tabId, url, forceReload = false) {
  const current = await chrome.tabs.get(tabId);
  const sameUrl = (current.url || "").split("#")[0] === url.split("#")[0];
  if (sameUrl && !forceReload) {
    if (current.status !== "complete") await waitForTabReady(tabId, url, 25000);
    return;
  }
  const ready = waitForTabReady(tabId, url, 25000, !forceReload);
  if (sameUrl) await chrome.tabs.reload(tabId);
  else await chrome.tabs.update(tabId, { url, active: false });
  try {
    await ready;
  } catch (error) {
    const latest = await chrome.tabs.get(tabId);
    const expected = new URL(url);
    const actual = new URL(latest.url || "about:blank");
    if (actual.hostname !== expected.hostname || actual.pathname !== expected.pathname) throw error;
  }
}

async function inspectPlatformPage(tabId, source) {
  let page;
  try {
    const [{ result } = {}] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => ({ href: location.href, title: document.title, readyState: document.readyState }),
    });
    page = result;
  } catch (error) {
    const accessError = new Error(`页面无法访问或被浏览器中止（ERR_FAILED）；请检查网络后重试。${error.message ? ` ${error.message}` : ""}`);
    accessError.code = isMissingTabError(error) ? "TAB_CLOSED" : "PLATFORM_PAGE_UNAVAILABLE";
    throw accessError;
  }
  if (!page?.href) {
    const accessError = new Error("页面没有成功加载，请重试");
    accessError.code = "PAGE_NOT_READY";
    throw accessError;
  }
  const access = DianAgentScanPolicy.inspectPageAccess(page, source);
  if (!access.ok) {
    const accessError = new Error(access.message);
    accessError.code = access.code;
    throw accessError;
  }
  return page;
}

async function activatePageTab(tabId, texts) {
  const wanted = (Array.isArray(texts) ? texts : [texts]).filter(Boolean);
  if (!wanted.length) return true;
  const [{ result = false } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    func: (wantedTexts) => {
      const candidates = Array.from(document.querySelectorAll("[role='tab'], button, [class*='tab']"));
      const target = candidates.find((element) => {
        const label = (element.innerText || "").trim();
        return element.getClientRects().length > 0 && wantedTexts.some((text) => label === text || (label.includes(text) && label.length <= text.length + 8));
      });
      if (!target) return false;
      target.click();
      return true;
    },
    args: [wanted],
  });
  return result;
}

async function rejectAfterForensicIdentityConflict(source, snapshot, options = {}) {
  if (!snapshotHasIdentityConflict(snapshot, source)) return false;
  const bridgeResult = await storeAndPush(source, snapshot, {
    expectedStoreKey: String(options.expectedStoreKey || ""),
    expectedAccountKey: String(options.expectedAccountKey || ""),
    runId: String(options.runId || ""),
    pageId: String(options.pageId || ""),
    attemptId: String(options.attemptId || ""),
  });
  const conflicts = Array.isArray(snapshot?.identity_conflicts) ? snapshot.identity_conflicts : [];
  const accountConflict = conflicts.some((kind) => /qianchuan_(?:advertiser|account)_id/.test(String(kind)));
  const error = new Error(bridgeResult?.error || (accountConflict
    ? "当前页面存在多个千川账号身份，候选数据仅已隔离取证。"
    : "当前页面包含多组店铺信息，本次数据已隔离，不会用于经营判断。"));
  error.code = bridgeResult?.error_code || (accountConflict ? "ACCOUNT_IDENTITY_CONFLICT" : "STORE_IDENTITY_CONFLICT");
  error.status = Number(bridgeResult?.status || 0);
  error.quarantined = bridgeResult?.quarantined === true;
  error.forensic_saved = bridgeResult?.forensic_saved === true;
  throw error;
}

async function scanOnePage(tabId, page, reason, expectedAccount = {}, expectedStoreKey = "", runId = "") {
  let lastError;
  const candidateUrls = [page.url, ...(page.fallbackUrls || [])];
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    try {
      const targetUrl = candidateUrls[Math.min(attempt - 1, candidateUrls.length - 1)];
      await navigateScanTab(tabId, targetUrl, attempt > 1);
      await inspectPlatformPage(tabId, page.source);
      await sleep(page.waitMs);
      if (page.tabText || page.tabTexts) {
        const wantedTabs = page.tabTexts || [page.tabText];
        const activated = await activatePageTab(tabId, wantedTabs);
        if (!activated) throw new Error(`未找到“${wantedTabs.join(" / ")}”页签`);
        await sleep(3500);
      }
      const scanMode = page.harvestList ? "full-scan-list" : "full-scan-page";
      const collectTimeoutMs = Number(page.collectTimeoutMs || (page.harvestList ? 30000 : 12000));
      // The page request id remains stable across the two browser attempts so
      // a committed /push whose response was lost is returned idempotently.
      const attemptId = `${runId || "legacy"}_${page.id}`;
      const response = await withTimeout(
        collectFromTab(page.source, { id: tabId }, `${scanMode}-${reason}-${page.id}`, {
          deferPush: true,
          runId,
          pageId: page.id,
          attemptId,
        }),
        collectTimeoutMs,
        `${page.label}采集超过 ${Math.round(collectTimeoutMs / 1000)} 秒，已跳过`,
        "PAGE_LOAD_TIMEOUT",
      );
      if (!response?.ok) {
        const collectionError = new Error(response?.error || "采集失败");
        collectionError.code = response?.error_code || response?.code || scanErrorCode(collectionError) || "CONTENT_SCRIPT_UNAVAILABLE";
        throw collectionError;
      }
      if (isScanCancelled(runId)) {
        const cancelled = new Error("巡店已由用户停止");
        cancelled.code = "SCAN_CANCELLED";
        throw cancelled;
      }
      if (!response.snapshot || typeof response.snapshot !== "object") throw new Error("页面未返回可校验的候选快照");
      await rejectAfterForensicIdentityConflict(page.source, response.snapshot, {
        expectedStoreKey,
        expectedAccountKey: page.source === "qianchuan" ? expectedAccount?.key || "" : "",
        runId,
        pageId: page.id,
        attemptId,
      });
      if (response.snapshot.quality?.login_required === true) {
        const loginError = new Error(`${page.source === "qianchuan" ? "千川" : "抖店"}登录已失效，请完成登录后手动续跑。`);
        loginError.code = "LOGIN_REQUIRED";
        throw loginError;
      }
      const expectedPageTypes = page.expectedPageTypes || [page.expectedPageType || page.id];
      if (!expectedPageTypes.includes(response.page_type)) {
        const pageTypeError = new Error(`页面识别为 ${response.page_type}，预期为 ${expectedPageTypes.join(" / ")}`);
        pageTypeError.code = "PAGE_TYPE_MISMATCH";
        pageTypeError.actual_page_type = response.page_type;
        pageTypeError.expected_page_types = expectedPageTypes;
        throw pageTypeError;
      }
      if (page.source === "doudian" && page.requiresIdentityProof === true) {
        const proof = DianAgentScanPolicy.inspectDoudianIdentityProof(response.snapshot, {
          pageType: response.page_type,
        });
        if (!proof.ok && proof.code !== "STORE_IDENTITY_CONFLICT") {
          const proofError = new Error("当前抖店首页暂时不能用于经营判断，本次数据没有使用。");
          proofError.code = proof.code || "STORE_IDENTITY_UNRESOLVED";
          throw proofError;
        }
      }
      if (page.source === "doudian") {
        requireDoudianSnapshotIdentity(response.snapshot, { allowConflictForForensics: true });
      }
      if (response.snapshot.quality?.pagination_stalled === true) {
        const stalledError = new Error("列表翻页后内容没有更新，已停止用旧页面覆盖本地快照。");
        stalledError.code = "COLLECTION_STALLED";
        throw stalledError;
      }
      const currentTab = await chrome.tabs.get(tabId);
      const prepared = page.source === "doudian" && page.requiresIdentityProof !== true
        ? DianAgentScanPolicy.applyDoudianScanContext(response.snapshot, doudianScanContexts.get(tabId), {
          source: page.source,
          tabUrl: currentTab.url || response.snapshot.url || "",
          runId,
          tabId,
        })
        : { snapshot: response.snapshot, applied: false, reason: "not_required" };
      if (
        page.source === "doudian"
        && page.requiresIdentityProof !== true
        && prepared.reason !== "fresh_identity_claim"
        && prepared.reason !== "identity_conflict"
      ) {
        if (prepared.reason === "expired") clearDoudianScanContext(tabId);
        const identityError = new Error(prepared.reason === "identity_conflict"
          ? "检测到店铺正在切换，本次已暂停。"
          : "本次页面范围已经失效，数据没有使用；请回到抖店首页后重试。");
        identityError.code = prepared.reason === "identity_conflict" ? "STORE_IDENTITY_CONFLICT" : "STORE_IDENTITY_UNRESOLVED";
        throw identityError;
      }
      const preparedSnapshot = prepared.snapshot;
      const bridgeResult = await storeAndPush(page.source, preparedSnapshot, {
        expectedStoreKey,
        expectedAccountKey: page.source === "qianchuan" ? expectedAccount?.key || "" : "",
        runId,
        pageId: page.id,
        attemptId,
      });
      if (!bridgeResult?.ok) {
        const bridgeError = new Error(bridgeResult?.error || "候选快照未通过本地校验");
        bridgeError.code = bridgeResult?.error_code || scanErrorCode(bridgeError);
        bridgeError.status = Number(bridgeResult?.status || 0);
        bridgeError.quarantined = bridgeResult?.quarantined === true;
        bridgeError.forensic_saved = bridgeResult?.forensic_saved === true;
        throw bridgeError;
      }
      const capturedAccount = bridgeResult.account || null;
      const capturedStore = bridgeResult.store || null;
      if (!capturedStore?.key) {
        const identityError = new Error("这个页面暂时不能用于经营判断，请打开抖店首页后重试。");
        identityError.code = "STORE_IDENTITY_UNRESOLVED";
        throw identityError;
      }
      if (expectedStoreKey && capturedStore.key !== expectedStoreKey) {
        const storeError = new Error("检测到店铺已切换，本次巡店已停止。请回到要经营的抖店首页后重试。");
        storeError.code = "STORE_MISMATCH";
        throw storeError;
      }
      if (page.source === "qianchuan" && expectedAccount?.key) {
        const accountMatch = matchAccount(capturedAccount, expectedAccount);
        if (!accountMatch.ok) {
          const accountError = new Error(accountMatch.message);
          accountError.code = accountMatch.code;
          throw accountError;
        }
      }
      if (response.page_type === "unknown") throw new Error("页面类型未识别");
      const planIdentity = DianAgentScanPolicy.inspectPlanIdentityCoverage(
        response.snapshot,
        response.page_type,
      );
      const harvestComplete = response.quality?.collection_complete !== false;
      const collectionComplete = harvestComplete && planIdentity.complete;
      return {
        id: page.id,
        label: page.label,
        source: page.source,
        ok: true,
        page_type: response.page_type,
        quality: response.quality || null,
        captured_at: Date.now(),
        account_key: capturedAccount?.key || "",
        account_label: capturedAccount?.label || "",
        account_confidence: capturedAccount?.confidence || "",
        account_identity_source: capturedAccount?.identity_source || "",
        store_key: capturedStore.key,
        store_confidence: capturedStore.confidence || "",
        store_identity_source: capturedStore.identity_source || "",
        collection_complete: collectionComplete,
        warning_code: !planIdentity.complete
          ? planIdentity.code
          : !harvestComplete ? "COLLECTION_TRUNCATED" : "",
        warning: !planIdentity.complete ? planIdentity.message : "",
        collection_pages: Number(response.quality?.pages_scanned || 1),
        pagination_truncated: response.quality?.pagination_truncated === true,
        virtual_scroll_truncated: response.quality?.virtual_scroll_truncated === true,
      };
    } catch (error) {
      lastError = error;
      if (isNonRetryableScanError(error)) break;
      if (attempt < 2) await sleep(1200);
    }
  }
  return {
    id: page.id,
    label: page.label,
    source: page.source,
    ok: false,
    captured_at: Date.now(),
    error: lastError?.message || String(lastError),
    error_code: scanErrorCode(lastError),
    status: Number(lastError?.status || 0),
    quarantined: lastError?.quarantined === true,
    forensic_saved: lastError?.forensic_saved === true,
  };
}

function isMissingTabError(error) {
  return /No tab with id|Invalid tab ID|tab.+(?:closed|not found)/i.test(String(error || ""));
}

async function findQianchuanSeedTab(expectedAccount = {}, expectedStoreKey = "") {
  const stored = await chrome.storage.local.get("qianchuanSeed");
  const preferredTabId = Number(stored.qianchuanSeed?.tab_id);
  const tabs = rankSeedTabs(await querySourceTabs("qianchuan"), Number.isInteger(preferredTabId) ? preferredTabId : null)
    .filter((tab) => Number.isInteger(tab.id));
  let fallbackTab = null;
  for (const tab of tabs.slice(0, 8)) {
    fallbackTab ||= tab;
    try {
      const response = await collectFromTab("qianchuan", tab, "identify-scan-seed", { deferPush: true });
      if (!response?.ok || !response.snapshot || response.page_type === "unknown") continue;
      const bridgeResult = await storeAndPush("qianchuan", response.snapshot, {
        expectedStoreKey,
        expectedAccountKey: expectedAccount?.key || "",
      });
      if (!bridgeResult?.ok) continue;
      const account = bridgeResult.account || null;
      if (!expectedAccount?.key || matchAccount(account, expectedAccount).ok) {
        return { tab, account };
      }
    } catch {
      // Try another already-open Qianchuan tab before falling back.
    }
  }
  if (expectedAccount?.key) {
    const error = new Error("没有找到已登录且与所选账号一致的千川页面。请先访问该账号的千川页面，点击工作台右侧“同步千川”，再开始巡检。");
    error.code = "ACCOUNT_MISMATCH";
    throw error;
  }
  return fallbackTab ? { tab: fallbackTab, account: null } : null;
}

async function discoverQianchuanRoutes(tabId) {
  try {
    const [{ result } = {}] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const current = { text: document.title || "", url: location.href };
        const links = Array.from(document.querySelectorAll("a[href], [data-href], [data-path]"))
          .filter((element) => element.getClientRects().length > 0)
          .map((element) => {
            const raw = element.href || element.getAttribute("data-href") || element.getAttribute("data-path") || "";
            let url = "";
            try { url = new URL(raw, location.origin).href; } catch { return null; }
            return { text: String(element.innerText || element.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim(), url };
          })
          .filter((item) => item?.url?.startsWith(location.origin))
          .slice(0, 300);
        return { current, links };
      },
    });
    return DianAgentScanPolicy.resolveQianchuanRoutes(result?.current, result?.links);
  } catch {
    return {};
  }
}

async function createScanTab(source = "", expectedAccount = {}, preparedSeed = null, expectedStoreKey = "") {
  if (source === "qianchuan") {
    let seed = preparedSeed;
    if (seed?.tab?.id) {
      try {
        await chrome.tabs.get(seed.tab.id);
      } catch {
        seed = null;
      }
    }
    seed ||= await findQianchuanSeedTab(expectedAccount, expectedStoreKey);
    if (seed?.tab?.id) {
      const routes = await discoverQianchuanRoutes(seed.tab.id);
      const duplicated = await chrome.tabs.duplicate(seed.tab.id);
      await chrome.tabs.update(duplicated.id, { active: false });
      return { tab: duplicated, account: seed.account || null, routes };
    }
  }
  return { tab: await chrome.tabs.create({ url: "about:blank", active: false }), account: null, routes: {} };
}

async function runFullScan(reason = "manual", pageIds = null, accountKey = "", storeKey = "", scanScope = "", runId = createScanRunId(), recoverySeed = null, signalInitialCheckpoint = null) {
  const startedAt = Date.now();
  await chrome.storage.local.set({ lastSyncAttemptAt: startedAt });
  const results = [];
  const isRecoveryRun = Boolean(recoverySeed && typeof recoverySeed === "object" && recoverySeed.run_id);
  const rootRunId = String(isRecoveryRun ? (recoverySeed.root_run_id || recoverySeed.run_id) : runId);
  const rootStartedAt = Number(isRecoveryRun ? (recoverySeed.root_started_at || recoverySeed.started_at || startedAt) : startedAt);
  const canonicalFullLaunch = scanScope === "full" && reason === "manual";
  const launchPageIds = DianAgentScanScopePolicy.resolveLaunchPageIds({
    scope: scanScope,
    reason,
    requestedPageIds: pageIds,
    accountKey,
  });
  const targeted = !canonicalFullLaunch && launchPageIds.length > 0;
  const productGraphScope = scanScope === "product_graph";
  const quickScope = productGraphScope || scanScope === "quick" || (!scanScope && targeted);
  const resolvedScope = productGraphScope ? "product_graph" : quickScope ? "quick" : "full";
  const previousScan = (await chrome.storage.local.get("fullScan")).fullScan || {};
  let requestedPages = launchPageIds.length
    ? FULL_SCAN_PAGES.filter((page) => launchPageIds.includes(page.id))
    : FULL_SCAN_PAGES;
  if (!requestedPages.length) throw new Error("本轮巡店没有有效页面，请重新选择巡店范围。");
  let scanPages = requestedPages;
  let plannedPageIds = isRecoveryRun && Array.isArray(recoverySeed.planned_page_ids) && recoverySeed.planned_page_ids.length
    ? DianAgentScanScopePolicy.uniqueKnownPageIds(recoverySeed.planned_page_ids)
    : requestedPages.map((page) => page.id);
  let lockedAccount = { key: String(accountKey || ""), label: "", identity_source: accountKey ? "selected" : "" };
  let accountMode = accountKey ? "fixed" : "doudian_only";
  let scanTab;
  let scanSource = "";
  let qianchuanRoutes = {};
  let qianchuanPreflight = null;
  let qianchuanPreflightError = null;
  const initialProgress = isRecoveryRun
    ? DianAgentScanPolicy.reconcileRecoveryProgress(
      { ...recoverySeed, planned_page_ids: plannedPageIds }, [],
      { storeKey, accountKey }, plannedPageIds,
    )
    : {
      planned_page_ids: plannedPageIds, results: [], attempted_page_ids: [], pending_page_ids: plannedPageIds,
      coverage_complete: false, success: 0, failed: 0, low_quality: 0,
    };
  const initialRecovery = buildRecoveryCheckpoint({
    status: "running", planned_page_ids: plannedPageIds,
    attempted_page_ids: initialProgress.attempted_page_ids,
    pending_page_ids: initialProgress.pending_page_ids,
    results: initialProgress.results,
  }, plannedPageIds);
  const initialCheckpointAccepted = await setFullScanState({
    status: "running", scope: resolvedScope,
    targeted_page_ids: isRecoveryRun ? (recoverySeed.targeted_page_ids || []) : targeted ? plannedPageIds : [],
    planned_page_ids: plannedPageIds, execution_page_ids: requestedPages.map((page) => page.id),
    attempted_page_ids: initialProgress.attempted_page_ids, pending_page_ids: initialProgress.pending_page_ids,
    reason, store_key: storeKey, account_mode: accountMode, account_key: lockedAccount.key,
    account_label: "", started_at: startedAt, root_started_at: rootStartedAt, finished_at: null,
    root_run_id: rootRunId, parent_run_id: isRecoveryRun ? String(recoverySeed.run_id || "") : "",
    current: "正在准备经营页面", index: initialProgress.attempted_page_ids.length, total: plannedPageIds.length,
    success: initialProgress.success, failed: initialProgress.failed, low_quality: initialProgress.low_quality,
    error: "", error_code: "", results: initialProgress.results,
    owned_tab_id: null, resumed_from_run_id: isRecoveryRun ? String(recoverySeed.run_id || "") : "",
    recovery: initialRecovery,
  }, runId, { replaceRun: true });
  if (typeof signalInitialCheckpoint === "function") signalInitialCheckpoint(initialCheckpointAccepted);
  if (!initialCheckpointAccepted) {
    return {
      status: "not_started",
      started: false,
      code: "SCAN_NOT_STARTED",
      run_id: runId,
      error: "本地 Agent 未接受本轮巡店启动检查点，巡店未启动。",
    };
  }
  chrome.alarms.create(SCAN_RECOVERY_ALARM, { delayInMinutes: 1, periodInMinutes: 1 });
  try {
    const catalogResponse = await fetchWithTimeout(`${BRIDGE_URL}/stores`, { cache: "no-store" }, 6000);
    if (!catalogResponse.ok) throw new Error("暂时无法准备经营数据，巡店已停止，请稍后重试。");
    const catalog = await catalogResponse.json();
    const confirmedStore = DianAgentScanPolicy.confirmSelectedStore(catalog, storeKey);
    if (!confirmedStore.ok) {
      const selectionError = new Error(confirmedStore.message);
      selectionError.code = confirmedStore.code;
      throw selectionError;
    }
    storeKey = confirmedStore.storeKey;
    // A recovery is a child attempt of the original receipt. Even an empty
    // account scope is locked; newly selecting a Qianchuan account must not
    // silently turn a 13-page root run into an 18-page run.
    accountKey = isRecoveryRun
      ? String(recoverySeed.account_key || "")
      : String(accountKey || catalog.selected_account_key || "");
    accountMode = accountKey ? "fixed" : "doudian_only";
    lockedAccount = { key: accountKey, label: "", identity_source: accountKey ? "selected" : "" };
    if (canonicalFullLaunch) {
      const canonicalPageIds = DianAgentScanScopePolicy.fullScanPageIds({ includeAds: Boolean(accountKey) });
      requestedPages = FULL_SCAN_PAGES.filter((page) => canonicalPageIds.includes(page.id));
    }
    scanPages = requestedPages.filter((page) => accountKey || page.source === "doudian");
    if (!isRecoveryRun) plannedPageIds = scanPages.map((page) => page.id);
    if (!scanPages.length) throw new Error("本轮没有可读取的页面，请先打开抖店或对应千川页面。");
    await setFullScanState({
      store_key: storeKey, account_mode: accountMode, account_key: lockedAccount.key,
      planned_page_ids: plannedPageIds,
      targeted_page_ids: isRecoveryRun ? (recoverySeed.targeted_page_ids || []) : targeted ? plannedPageIds : [],
      execution_page_ids: scanPages.map((page) => page.id),
      pending_page_ids: isRecoveryRun ? initialProgress.pending_page_ids : plannedPageIds,
      total: plannedPageIds.length,
      current: scanPages.some((page) => page.source === "qianchuan") ? "正在准备千川页面" : "抖店已准备，开始巡店",
    }, runId);
    if (scanPages.some((page) => page.source === "qianchuan")) {
      try {
        qianchuanPreflight = await findQianchuanSeedTab(lockedAccount, storeKey);
        if (!qianchuanPreflight?.tab?.id) {
          const accountError = new Error("没有找到已登录的千川页面，请先打开对应账号的巨量千川。");
          accountError.code = "LOGIN_REQUIRED";
          throw accountError;
        }
      } catch (error) {
        qianchuanPreflightError = error;
        const doudianPages = scanPages.filter((page) => page.source === "doudian");
        if (!doudianPages.length) throw error;
        scanPages = doudianPages;
        await setFullScanState({
          current: "千川页面暂不可用，本轮先完成抖店巡查",
          error: error.message || String(error), error_code: scanErrorCode(error),
        }, runId);
      }
    }
    for (let index = 0; index < scanPages.length; index += 1) {
      if (isScanCancelled(runId)) break;
      const page = scanPages[index];
      if (!scanTab?.id || page.source !== scanSource) {
        if (scanTab?.id) {
          clearDoudianScanContext(scanTab.id);
          await chrome.tabs.remove(scanTab.id).catch(() => undefined);
        }
        const created = await createScanTab(page.source, lockedAccount, page.source === "qianchuan" ? qianchuanPreflight : null, storeKey);
        if (page.source === "qianchuan") qianchuanPreflight = null;
        scanTab = created.tab;
        activeFullScanTabId = scanTab.id;
        scanSource = page.source;
        if (page.source === "doudian") {
          await setFullScanState({ current: "正在准备当前抖店" }, runId);
          await establishDoudianScanContext(scanTab.id, storeKey, runId);
        }
        if (page.source === "qianchuan") qianchuanRoutes = created.routes || {};
        if (
          page.source === "qianchuan"
          && created.account
          && !lockedAccount.key
          && (created.account.identity_source === "platform_id" || created.account.confidence === "high")
        ) {
          lockedAccount = created.account;
          await selectAnalysisAccount(lockedAccount.key);
          await setFullScanState({ account_key: lockedAccount.key, account_label: lockedAccount.label || "" }, runId);
        }
        await setFullScanState({ owned_tab_id: scanTab.id }, runId);
      }
      const liveAttemptedCount = isRecoveryRun
        ? new Set([...initialProgress.attempted_page_ids, ...results.map((item) => item.id)]).size
        : results.length;
      await setFullScanState({ current: page.label, index: liveAttemptedCount }, runId);
      const discoveredUrl = page.source === "qianchuan" ? qianchuanRoutes[page.id] : "";
      const scanPage = discoveredUrl ? { ...page, url: discoveredUrl, fallbackUrls: [page.url, ...(page.fallbackUrls || [])] } : page;
      let result = await scanOnePage(scanTab.id, scanPage, reason, lockedAccount, storeKey, runId);
      if (!isScanCancelled(runId) && !result.ok && isMissingTabError(result.error)) {
        clearDoudianScanContext(scanTab.id);
        const recovered = await createScanTab(page.source, lockedAccount, null, storeKey);
        scanTab = recovered.tab;
        if (page.source === "doudian") await establishDoudianScanContext(scanTab.id, storeKey, runId);
        activeFullScanTabId = scanTab.id;
        await setFullScanState({ owned_tab_id: scanTab.id }, runId);
        result = await scanOnePage(scanTab.id, scanPage, `${reason}-tab-recovered`, lockedAccount, storeKey, runId);
      }
      if (isScanCancelled(runId)) break;
      if (page.source === "qianchuan" && result.ok && result.account_key) {
        const detectedAccount = {
          key: result.account_key,
          label: result.account_label || "",
          confidence: result.account_confidence || "",
          identity_source: result.account_identity_source || "",
        };
        const shouldLock = !lockedAccount.key
          && (detectedAccount.identity_source === "platform_id" || detectedAccount.confidence === "high");
        const shouldUpgrade = accountMode === "auto"
          && lockedAccount.identity_source !== "platform_id"
          && detectedAccount.identity_source === "platform_id"
          && matchAccount(detectedAccount, lockedAccount).ok;
        if (shouldLock || shouldUpgrade) {
          lockedAccount = detectedAccount;
          await selectAnalysisAccount(lockedAccount.key);
          await setFullScanState({ account_key: lockedAccount.key, account_label: lockedAccount.label }, runId);
        } else if (!lockedAccount.label && result.account_key === lockedAccount.key) {
          lockedAccount.label = result.account_label || "";
          lockedAccount.identity_source = result.account_identity_source || lockedAccount.identity_source;
          await setFullScanState({ account_label: lockedAccount.label }, runId);
        }
      }
      results.push(result);
      const progress = isRecoveryRun
        ? DianAgentScanPolicy.reconcileRecoveryProgress(
          { ...recoverySeed, planned_page_ids: plannedPageIds }, results,
          { storeKey, accountKey: lockedAccount.key || "" }, plannedPageIds,
        )
        : {
          results,
          attempted_page_ids: results.map((item) => item.id),
          pending_page_ids: plannedPageIds.filter((id) => !results.some((item) => item.id === id)),
          success: results.filter((item) => item.ok).length,
          failed: results.filter((item) => !item.ok).length,
          low_quality: results.filter((item) => item.ok && Number(item.quality?.score || 0) < 50).length,
        };
      const recovery = buildRecoveryCheckpoint({
        status: "running",
        planned_page_ids: plannedPageIds,
        attempted_page_ids: progress.attempted_page_ids,
        pending_page_ids: progress.pending_page_ids,
        results: progress.results,
        error_code: result.error_code || result.warning_code || "",
      }, plannedPageIds);
      await setFullScanState({
        results: progress.results,
        attempted_page_ids: progress.attempted_page_ids,
        pending_page_ids: progress.pending_page_ids,
        index: progress.attempted_page_ids.length,
        success: progress.success,
        failed: progress.failed,
        low_quality: progress.low_quality,
        recovery,
      }, runId);
      if (shouldStopAfterResult(result)) break;
      // Give the browser UI and the platform page a short idle window between
      // pages so a long scan does not monopolize the renderer.
      await sleep(350);
    }
    let finalResults = results;
    let finalProgress = null;
    if (isRecoveryRun) {
      finalProgress = DianAgentScanPolicy.reconcileRecoveryProgress(
        { ...recoverySeed, planned_page_ids: plannedPageIds }, results,
        { storeKey, accountKey: lockedAccount.key || "" }, plannedPageIds,
      );
      finalResults = finalProgress.results;
    } else if (targeted && !isScanCancelled(runId)) {
      finalResults = mergeScopedResults(
        previousScan,
        results,
        { storeKey, accountKey: lockedAccount.key || "" },
        FULL_SCAN_PAGES.map((page) => page.id),
      );
    }
    const attemptedPageIds = finalProgress?.attempted_page_ids || results.map((item) => item.id);
    const finalByPageId = new Map(finalResults.filter((item) => item?.id).map((item) => [String(item.id), item]));
    // Targeted refreshes retain historical results for the dashboard, but all
    // completion counters and recovery decisions are scoped to this run's
    // page contract. Otherwise an old unrelated failure can make success
    // exceed total or incorrectly fail a one-page refresh.
    const scopedFinalResults = plannedPageIds.map((id) => finalByPageId.get(id)).filter(Boolean);
    const pendingPageIds = finalProgress?.pending_page_ids || plannedPageIds.filter((id) => {
      const result = finalByPageId.get(id);
      return !attemptedPageIds.includes(id) || !result || result.ok === false || result.collection_complete === false;
    });
    const attemptedFailed = scopedFinalResults.some((item) => !item.ok);
    const collectionIncomplete = scopedFinalResults.some((item) => item.ok && item.collection_complete === false);
    const fatalResult = scopedFinalResults.find((item) => shouldStopAfterResult(item));
    const failedResult = scopedFinalResults.find((item) => !item.ok);
    const incompleteResult = scopedFinalResults.find((item) => item.ok && item.collection_complete === false);
    const finalError = fatalResult?.error || failedResult?.error || incompleteResult?.warning
      || qianchuanPreflightError?.message || "";
    const finalErrorCode = fatalResult?.error_code || failedResult?.error_code
      || incompleteResult?.warning_code || scanErrorCode(qianchuanPreflightError);
    const status = isScanCancelled(runId) ? "cancelled" : attemptedFailed || collectionIncomplete || pendingPageIds.length ? "partial" : "completed";
    const recovery = buildRecoveryCheckpoint({
      status,
      planned_page_ids: plannedPageIds,
      attempted_page_ids: attemptedPageIds,
      pending_page_ids: pendingPageIds,
      results: finalResults,
      error_code: finalErrorCode,
    }, plannedPageIds);
    await setFullScanState({
      status, scope: resolvedScope, coverage_complete: pendingPageIds.length === 0 && !attemptedFailed && !collectionIncomplete,
      current: "", finished_at: Date.now(), error: finalError,
      error_code: finalErrorCode, results: finalResults, index: attemptedPageIds.length,
      total: plannedPageIds.length, attempted_page_ids: attemptedPageIds, pending_page_ids: pendingPageIds,
      success: scopedFinalResults.filter((item) => item.ok && item.collection_complete !== false).length,
      failed: scopedFinalResults.filter((item) => !item.ok).length,
      low_quality: scopedFinalResults.filter((item) => item.ok && Number(item.quality?.score || 0) < 50).length,
      owned_tab_id: null,
      recovery,
    }, runId);
    if (scopedFinalResults.some((item) => item.ok && item.collection_complete !== false)) {
      const successfulAt = Date.now();
      await chrome.storage.local.set({ lastSyncAttempt: successfulAt, lastSuccessfulSync: successfulAt });
    }
    if (!isScanCancelled(runId)) {
      await fetchWithTimeout(`${BRIDGE_URL}/reports/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
        body: "{}",
      }, 8000).catch(() => undefined);
    }
    return { status, results: finalResults };
  } catch (error) {
    const progress = isRecoveryRun
      ? DianAgentScanPolicy.reconcileRecoveryProgress(
        { ...recoverySeed, planned_page_ids: plannedPageIds }, results,
        { storeKey, accountKey: lockedAccount.key || "" }, plannedPageIds,
      )
      : {
        results,
        attempted_page_ids: results.map((item) => item.id),
        pending_page_ids: plannedPageIds.filter((id) => !results.some((item) => item.id === id)),
        success: results.filter((item) => item.ok).length,
        failed: results.filter((item) => !item.ok).length,
      };
    const status = isScanCancelled(runId) ? "cancelled" : "error";
    const recovery = buildRecoveryCheckpoint({
      status,
      planned_page_ids: plannedPageIds,
      attempted_page_ids: progress.attempted_page_ids,
      pending_page_ids: progress.pending_page_ids,
      results: progress.results,
      error_code: scanErrorCode(error),
    }, plannedPageIds);
    await setFullScanState({
      status, current: "", finished_at: Date.now(),
      error: error.message || String(error), error_code: scanErrorCode(error), results: progress.results,
      attempted_page_ids: progress.attempted_page_ids, pending_page_ids: progress.pending_page_ids,
      index: progress.attempted_page_ids.length, total: plannedPageIds.length,
      success: progress.success,
      failed: progress.failed, owned_tab_id: null,
      recovery,
    }, runId);
    if (Number(progress.success || 0) > 0) {
      const successfulAt = Date.now();
      await chrome.storage.local.set({ lastSyncAttempt: successfulAt, lastSuccessfulSync: successfulAt });
    }
    return { status, error: error.message || String(error), results: progress.results };
  } finally {
    if (scanTab?.id) {
      clearDoudianScanContext(scanTab.id);
      await chrome.tabs.remove(scanTab.id).catch(() => undefined);
    }
    if (activeFullScanRunId === runId) activeFullScanTabId = null;
    cancelledFullScanRuns.delete(runId);
  }
}

function startFullScan(reason = "manual", pageIds = null, accountKey = "", storeKey = "", scanScope = "", recoverySeed = null) {
  if (fullScanPromise) return fullScanPromise;
  assertVersionReloadAllowsNewWork("full_scan");
  if (!fullScanPromise) {
    const runId = createScanRunId();
    activeFullScanRunId = runId;
    let settleInitialCheckpoint;
    let initialCheckpointSettled = false;
    const initialCheckpointAccepted = new Promise((resolve) => { settleInitialCheckpoint = resolve; });
    const signalInitialCheckpoint = (accepted) => {
      if (initialCheckpointSettled) return;
      initialCheckpointSettled = true;
      settleInitialCheckpoint(accepted === true);
    };
    const completion = runFullScan(
      reason, pageIds, accountKey, storeKey, scanScope, runId, recoverySeed, signalInitialCheckpoint,
    );
    completion.catch(() => signalInitialCheckpoint(false));
    const tracked = completion.finally(() => {
      if (activeFullScanRunId === runId) {
        activeFullScanRunId = "";
        activeFullScanTabId = null;
        chrome.alarms.clear(SCAN_RECOVERY_ALARM).catch(() => undefined);
      }
      if (fullScanPromise === tracked) fullScanPromise = null;
    });
    tracked.run_id = runId;
    tracked.initial_checkpoint_accepted = initialCheckpointAccepted;
    fullScanPromise = tracked;
  }
  return fullScanPromise;
}

async function awaitFullScanLaunch(operation) {
  const accepted = await operation.initial_checkpoint_accepted;
  if (accepted) return { ok: true, started: true, run_id: operation.run_id };
  try {
    const result = await operation;
    return {
      ok: false,
      started: false,
      code: String(result?.code || "SCAN_NOT_STARTED"),
      run_id: String(result?.run_id || operation.run_id || ""),
      error: String(result?.error || "本地 Agent 未接受本轮巡店，巡店未启动。"),
    };
  } catch (error) {
    return {
      ok: false,
      started: false,
      code: String(error?.code || "SCAN_NOT_STARTED"),
      run_id: String(operation.run_id || ""),
      error: String(error?.message || error || "巡店启动失败。"),
    };
  }
}

async function recoverInterruptedScanUnlocked(reason = "recovery") {
  if (fullScanPromise) return { started: false, run_id: activeFullScanRunId, code: "SCAN_BUSY" };
  const stored = await chrome.storage.local.get("fullScan");
  const scan = stored.fullScan || {};
  if (!["running", "interrupted"].includes(scan.status)) return { started: false, code: "NO_INTERRUPTED_SCAN" };
  const planned = Array.isArray(scan.planned_page_ids) && scan.planned_page_ids.length
    ? scan.planned_page_ids
    : Array.isArray(scan.targeted_page_ids) && scan.targeted_page_ids.length
      ? scan.targeted_page_ids
      : DianAgentScanScopePolicy.fullScanPageIds({ includeAds: Boolean(scan.account_key) });
  const pagesToResume = resumePageIds(scan, planned);
  if (Number.isInteger(scan.owned_tab_id)) {
    clearDoudianScanContext(scan.owned_tab_id);
    await chrome.tabs.remove(scan.owned_tab_id).catch(() => undefined);
  }
  if (!pagesToResume.length) {
    const status = (scan.results || []).some((item) => item?.ok === false || item?.collection_complete === false) ? "partial" : "completed";
    const recovery = buildRecoveryCheckpoint({ ...scan, status, owned_tab_id: null }, planned);
    await setFullScanState({
      status, current: "", finished_at: Date.now(), owned_tab_id: null, recovery,
    }, String(scan.run_id || ""));
    return { started: false, code: "NO_PENDING_PAGES" };
  }
  const interrupted = {
    ...scan,
    status: "interrupted",
    interrupted: true,
    error_code: scan.error_code || "SCAN_INTERRUPTED",
    pending_page_ids: pagesToResume,
  };
  const recovery = buildRecoveryCheckpoint(interrupted, planned);
  if (scan.status === "running" || Number.isInteger(scan.owned_tab_id)) {
    await setFullScanState({
      status: "interrupted",
      current: "巡店断点已保留，等待手动续跑",
      interrupted_at: Date.now(),
      interruption_reason: reason,
      error: scan.error || recovery.message,
      error_code: scan.error_code || "SCAN_INTERRUPTED",
      pending_page_ids: pagesToResume,
      owned_tab_id: null,
      recovery,
    }, String(scan.run_id || ""));
  }
  await chrome.alarms.clear(SCAN_RECOVERY_ALARM).catch(() => undefined);
  return {
    started: false,
    code: "SCAN_PAUSED_FOR_USER",
    run_id: scan.run_id || "",
    total: pagesToResume.length,
    recovery,
  };
}

function recoverInterruptedScan(reason = "recovery") {
  if (scanRecoveryPromise) return scanRecoveryPromise;
  const tracked = (async () => {
    await ensureVersionReloadStateHydrated();
    assertVersionReloadAllowsNewWork("scan_recovery");
    return recoverInterruptedScanUnlocked(reason);
  })().finally(() => {
    if (scanRecoveryPromise === tracked) scanRecoveryPromise = null;
  });
  scanRecoveryPromise = tracked;
  return tracked;
}

async function querySourceTabs(source) {
  return chrome.tabs.query({ url: SOURCE_PATTERNS[source] });
}

async function collectFromTab(source, tab, reason, options = {}) {
  try {
    return await chrome.tabs.sendMessage(tab.id, {
      type: "collect-now",
      reason,
      defer_push: options.deferPush === true,
      run_id: options.runId || "",
      page_id: options.pageId || "",
      attempt_id: options.attemptId || "",
      target_plan_id: options.targetPlanId || "",
    });
  } catch (firstError) {
    const platformScript = source === "doudian" ? "content-doudian.js" : "content-qianchuan.js";
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: ["content-common.js", platformScript],
    });
    await new Promise((resolve) => setTimeout(resolve, 250));
    try {
      return await chrome.tabs.sendMessage(tab.id, {
        type: "collect-now",
        reason: `${reason}-reinjected`,
        defer_push: options.deferPush === true,
        run_id: options.runId || "",
        page_id: options.pageId || "",
        attempt_id: options.attemptId || "",
        target_plan_id: options.targetPlanId || "",
      });
    } catch (secondError) {
      throw new Error(`${firstError.message || firstError}; 重新注入后仍失败：${secondError.message || secondError}`);
    }
  }
}

async function syncSource(source, reason = "manual") {
  const tabs = await querySourceTabs(source);
  if (!tabs.length) {
    await updateStatus(source, "未打开对应后台页面", "warning");
    return { source, tabs: 0, collected: 0, errors: [] };
  }

  let collected = 0;
  const errors = [];
  for (const tab of tabs.slice(0, 8)) {
    try {
      const response = await collectFromTab(source, tab, reason);
      const bridgeResult = response?.bridge;
      if (response?.ok === true && bridgeResult?.ok === true) {
        collected += 1;
      } else {
        errors.push(bridgeResult?.error || response?.error || `标签页 ${tab.id} 未返回可接受的数据`);
      }
    } catch (error) {
      errors.push(error.message || String(error));
    }
  }

  if (collected && !errors.length) {
    await updateStatus(source, `已同步 ${collected} 个页面`, "ok");
  } else if (collected) {
    await updateStatus(source, `已同步 ${collected} 个页面，${errors.length} 个页面被拒绝`, "warning");
  } else {
    await updateStatus(source, "页面存在，但采集脚本未就绪；请刷新页面", "error");
  }
  return { source, tabs: tabs.length, collected, errors };
}

async function syncAll(reason = "manual") {
  await chrome.storage.local.set({ lastSyncAttemptAt: Date.now() });
  const results = await Promise.all([
    syncSource("doudian", reason),
    syncSource("qianchuan", reason),
  ]);
  if (results.some((item) => Number(item.collected || 0) > 0)) {
    const successfulAt = Date.now();
    await chrome.storage.local.set({ lastSyncAttempt: successfulAt, lastSuccessfulSync: successfulAt });
  }
  return results;
}

function normalizePurposeSyncOptions(options = {}) {
  const rawPageTypes = Array.isArray(options.expectedPageTypes)
    ? options.expectedPageTypes
    : typeof options.expectedPageTypes === "string"
      ? options.expectedPageTypes.split(",")
      : [];
  const expectedPageTypes = [...new Set(rawPageTypes
    .map((value) => String(value || "").trim().toLowerCase())
    .filter((value) => /^[a-z0-9_-]{1,64}$/.test(value)))];
  const expectedAccountKey = String(options.expectedAccountKey || "").trim().toLowerCase().slice(0, 128);
  const expectedStoreCandidate = String(options.expectedStoreKey || "").trim().toLowerCase();
  const expectedStoreKey = /^[a-z0-9_-]{1,80}$/.test(expectedStoreCandidate) ? expectedStoreCandidate : "";
  const purpose = String(options.purpose || "manual")
    .trim().toLowerCase().replace(/[^\p{L}\p{N}_-]+/gu, "-").replace(/^-+|-+$/g, "").slice(0, 64) || "manual";
  return {
    expectedPageTypes,
    expectedAccountKey,
    expectedStoreKey,
    purpose,
    requireHardReload: options.requireHardReload === true,
    sessionStartedAtMs: Math.max(0, Number(options.sessionStartedAtMs) || 0),
  };
}

function purposeSyncError(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

async function issueCurrentPageStoreGrant() {
  try {
    const response = await bridgeFetch("/stores/current-page-grant", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
      body: "{}",
    });
    const payload = await responseJson(response);
    if (!response.ok) return "";
    const token = String(payload.current_page_token || "");
    return /^[a-f0-9]{64}$/i.test(token) ? token : "";
  } catch {
    // Older Agents do not expose this capability. The extension still uses
    // only the accepted current-page store key and explicitly selects it.
    return "";
  }
}

async function syncCurrentPage(sourceOnly = "", options = {}) {
  await chrome.storage.local.set({ lastSyncAttemptAt: Date.now() });
  const purposeOptions = normalizePurposeSyncOptions(options);
  const requestedTabId = Number(options.targetTabId);
  const requestedTab = Number.isInteger(requestedTabId) && requestedTabId > 0
    ? await chrome.tabs.get(requestedTabId).catch(() => null)
    : null;
  const [activeTab] = requestedTab ? [null] : await chrome.tabs.query({ active: true, currentWindow: true });
  let targetTab = requestedTab || activeTab || null;
  let matchedBy = requestedTab ? "trusted-sender" : "active";
  if (sourceOnly === "qianchuan") {
    const [tabs, stored] = await Promise.all([
      querySourceTabs("qianchuan"),
      chrome.storage.local.get(["recentQianchuanTab", "qianchuanSeed"]),
    ]);
    const scopeJob = { store_key: purposeOptions.expectedStoreKey, account_key: purposeOptions.expectedAccountKey };
    const scopedRecords = purposeOptions.expectedStoreKey && purposeOptions.expectedAccountKey
      ? [stored.recentQianchuanTab, stored.qianchuanSeed].filter((record) => qianchuanReadbackScopeMatches(record, scopeJob))
      : [];
    const scopedTab = scopedRecords
      .map((record) => tabs.find((tab) => Number(tab?.id) === Number(record?.tab_id)))
      .find((tab) => tab?.id && DianAgentScanPolicy.isQianchuanUrl(tab.url));
    if (requestedTab && DianAgentScanPolicy.isQianchuanUrl(requestedTab.url)) {
      targetTab = requestedTab;
      matchedBy = "trusted-sender";
    } else if (scopedTab) {
      targetTab = scopedTab;
      matchedBy = "verified-scope";
    } else if (!DianAgentScanPolicy.isQianchuanUrl(activeTab?.url)) {
      const selection = DianAgentScanPolicy.selectQianchuanSyncTab(
        tabs,
        activeTab,
        Number(stored.recentQianchuanTab?.tab_id),
        Number(stored.qianchuanSeed?.tab_id),
      );
      targetTab = selection.tab;
      matchedBy = selection.matchedBy;
    }
  }
  const url = targetTab?.url || "";
  const source = url.startsWith("https://fxg.jinritemai.com/") ? "doudian"
    : url.startsWith("https://qianchuan.jinritemai.com/") || url.startsWith("https://buyin.jinritemai.com/") ? "qianchuan" : "";
  if (!targetTab?.id || !source) {
    throw new Error(sourceOnly === "qianchuan"
      ? "没有找到可同步的千川标签页，请先打开巨量千川任意页面"
      : "当前页面不是抖店或巨量千川后台");
  }
  if (sourceOnly && source !== sourceOnly) throw new Error("请先打开需要读取的巨量千川页面");
  await inspectPlatformPage(targetTab.id, source);
  if (source === "qianchuan" && purposeOptions.requireHardReload) {
    targetTab = await reloadExecutionReadbackTab(targetTab, {
      started_at_ms: purposeOptions.sessionStartedAtMs,
    });
  }
  const currentPageToken = source === "doudian" ? await issueCurrentPageStoreGrant() : "";
  // Current-page sync is always returned as a candidate so this worker can
  // attach the one-time page grant and validate the exact accepted receipt.
  const deferPush = true;
  const response = await collectFromTab(source, targetTab, `manual-current-page-${purposeOptions.purpose}`, { deferPush });
  if (!response?.ok) throw new Error(response?.error || "当前页面读取失败");
  const responsePageType = String(response.page_type || "unknown").trim().toLowerCase();
  const snapshotPageType = String(response.snapshot?.page_type || "unknown").trim().toLowerCase();
  const pageType = deferPush ? snapshotPageType : responsePageType;
  let bridgeResult = response.bridge || null;
  if (!deferPush && bridgeResult?.ok !== true) {
    throw purposeSyncError(
      bridgeResult?.error_code || "SYNC_REJECTED",
      bridgeResult?.error || "当前页面未通过本地校验，未保存。",
    );
  }
  if (deferPush) {
    if (!response.snapshot || typeof response.snapshot !== "object") {
      throw purposeSyncError("SYNC_CANDIDATE_MISSING", "当前页面未返回可校验数据，未保存；请刷新页面后重试。");
    }
    if (responsePageType !== "unknown" && responsePageType !== snapshotPageType) {
      throw purposeSyncError("PAGE_TYPE_CONFLICT", "当前页面返回了互相冲突的页面类型，未保存；请刷新页面后重试。");
    }
    if (pageType === "unknown") {
      throw purposeSyncError("PAGE_TYPE_UNRECOGNIZED", source === "qianchuan"
        ? "当前千川页面无法读取；请打开明确的计划、直播或素材页面后重试。"
        : "当前抖店页面无法读取；请打开抖店首页后重试。");
    }
    if (purposeOptions.expectedPageTypes.length && !purposeOptions.expectedPageTypes.includes(pageType)) {
      throw purposeSyncError(
        "PAGE_TYPE_MISMATCH",
        `本次 ${purposeOptions.purpose} 同步读取到“${pageType}”，需要“${purposeOptions.expectedPageTypes.join(" / ")}”；错误页面未保存，请打开正确页面后重试。`,
      );
    }
    bridgeResult = await storeAndPush(source, response.snapshot, {
      expectedStoreKey: purposeOptions.expectedStoreKey,
      expectedAccountKey: purposeOptions.expectedAccountKey,
      currentPageToken,
    });
    if (!bridgeResult?.ok) {
      const failure = purposeSyncError(
        bridgeResult?.error_code || "SYNC_REJECTED",
        bridgeResult?.error || "当前千川页面未通过本地校验，未保存。",
      );
      throw failure;
    }
  }
  const capturedAccount = bridgeResult?.account || response.account || null;
  const successfulAt = Date.now();
  const updates = { lastSyncAttempt: successfulAt, lastSuccessfulSync: successfulAt };
  if (source === "qianchuan") {
    updates.qianchuanSeed = {
      tab_id: targetTab.id,
      url: targetTab.url || "",
      account: capturedAccount,
      store: bridgeResult?.store || null,
      account_key: capturedAccount?.key || "",
      store_key: bridgeResult?.store?.key || capturedAccount?.store_key || "",
      captured_at: Date.now(),
    };
    updates.recentQianchuanTab = {
      tab_id: targetTab.id,
      window_id: targetTab.windowId,
      url: targetTab.url || "",
      title: targetTab.title || "",
      account_key: capturedAccount?.key || "",
      store_key: bridgeResult?.store?.key || capturedAccount?.store_key || "",
      last_seen: Date.now(),
    };
  }
  await chrome.storage.local.set(updates);
  return {
    source,
    page_type: pageType,
    quality: response.quality,
    account: capturedAccount,
    store: bridgeResult?.store || null,
    store_auto_confirmed: bridgeResult?.store_auto_confirmed === true,
    selected_store_key: String(bridgeResult?.selected_store_key || ""),
    selected_account_key: String(bridgeResult?.selected_account_key || ""),
    purpose: purposeOptions.purpose,
    expected_page_types: purposeOptions.expectedPageTypes,
    tab: { id: targetTab.id, title: targetTab.title || "", url: targetTab.url || "", matched_by: matchedBy },
  };
}

async function recordPageSyncReceipt(sourceValue, patch = {}) {
  const source = ["doudian", "qianchuan"].includes(String(sourceValue || "")) ? String(sourceValue) : "";
  if (!source) return;
  await mutateLocalStorage([PAGE_SYNC_RECEIPTS_KEY], (stored) => {
    const receipts = stored[PAGE_SYNC_RECEIPTS_KEY] && typeof stored[PAGE_SYNC_RECEIPTS_KEY] === "object"
      ? { ...stored[PAGE_SYNC_RECEIPTS_KEY] }
      : {};
    receipts[source] = { ...(receipts[source] || {}), ...patch };
    return { [PAGE_SYNC_RECEIPTS_KEY]: receipts };
  });
}

async function syncCurrentPageWithReceipt(sourceOnly = "", options = {}) {
  const attemptedAt = Date.now();
  try {
    const result = await syncCurrentPage(sourceOnly, options);
    const source = String(result?.source || sourceOnly || "");
    await recordPageSyncReceipt(source, {
      status: "success",
      last_attempt_at: attemptedAt,
      last_success_at: Date.now(),
      page_type: String(result?.page_type || ""),
      account_label: String(result?.account?.label || ""),
      account_key: String(result?.account?.key || ""),
      store_key: String(result?.store?.key || ""),
      message: "",
      error_code: "",
    }).catch(() => undefined);
    return result;
  } catch (error) {
    await recordPageSyncReceipt(sourceOnly, {
      status: "error",
      last_attempt_at: attemptedAt,
      message: String(error?.message || error || "同步失败"),
      error_code: String(error?.code || ""),
    }).catch(() => undefined);
    throw error;
  }
}

async function loadExecutionReadbackJobs() {
  const stored = await chrome.storage.local.get(EXECUTION_READBACK_STORAGE_KEY);
  const value = stored[EXECUTION_READBACK_STORAGE_KEY];
  return value && typeof value === "object" ? value : {};
}

function executionJournalRank(job = {}) {
  if (["verified", "failed", "uncertain"].includes(job.status)) return 6;
  return {
    awaiting_consume: 1,
    awaiting_submission: 2,
    submission_unknown: 3,
    awaiting_receipt: 4,
    readback_pending: 5,
  }[job.journal_state] || 5;
}

async function persistExecutionReadbackJob(jobValue, options = {}) {
  const policy = globalThis.DianExecutionReadbackPolicy;
  const job = policy.normalizeJob(jobValue);
  await mutateLocalStorage([EXECUTION_READBACK_STORAGE_KEY], (stored) => {
    const jobs = stored[EXECUTION_READBACK_STORAGE_KEY] && typeof stored[EXECUTION_READBACK_STORAGE_KEY] === "object"
      ? { ...stored[EXECUTION_READBACK_STORAGE_KEY] }
      : {};
    const existing = jobs[job.action_id] && typeof jobs[job.action_id] === "object"
      ? policy.normalizeJob(jobs[job.action_id])
      : null;
    const manualReopen = options.allowTerminalReopen === true
      && existing?.status === "uncertain"
      && job.status === "pending"
      && Number(job.manual_attempt_count || 0) > Number(existing.manual_attempt_count || 0);
    const keepExisting = existing && !manualReopen && (
      executionJournalRank(existing) > executionJournalRank(job)
      || (executionJournalRank(existing) === executionJournalRank(job)
        && Number(existing.updated_at_ms || 0) > Number(job.updated_at_ms || 0))
    );
    jobs[job.action_id] = keepExisting ? existing : job;
    const ordered = Object.entries(jobs)
      .sort(([, left], [, right]) => Number(right?.updated_at_ms || 0) - Number(left?.updated_at_ms || 0));
    // Never evict an unresolved at-most-once journal merely because newer
    // accounts executed more actions. Losing a pending/uncertain record would
    // leave the backend lock without a recoverable browser-side witness.
    const protectedEntries = ordered.filter(([, item]) => ["pending", "uncertain"].includes(policy.normalizeJob(item).status));
    const terminalEntries = ordered.filter(([, item]) => !["pending", "uncertain"].includes(policy.normalizeJob(item).status))
      .slice(0, Math.max(0, 100 - protectedEntries.length));
    const trimmed = Object.fromEntries([...protectedEntries, ...terminalEntries]);
    return { [EXECUTION_READBACK_STORAGE_KEY]: trimmed };
  });
  const jobs = await loadExecutionReadbackJobs();
  if (Object.values(jobs).some((item) => item?.status === "pending")) {
    chrome.alarms.create(EXECUTION_READBACK_ALARM, { delayInMinutes: 1, periodInMinutes: 1 });
  } else {
    await chrome.alarms.clear(EXECUTION_READBACK_ALARM);
  }
  return policy.normalizeJob(jobs[job.action_id] || job);
}

async function executionReceiptAlreadyAcknowledged(jobValue) {
  const job = globalThis.DianExecutionReadbackPolicy.normalizeJob(jobValue);
  const receipt = job.execution_receipt;
  if (!receipt || typeof receipt !== "object") return false;
  const response = await bridgeFetch(`${BRIDGE_URL}/actions/preflight`, { cache: "no-store" });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) return false;
  const session = payload.session;
  const expectedState = receipt.submitted === false ? "failed" : "executing";
  return Boolean(
    session
    && String(session.action_id || "").toLowerCase() === job.action_id
    && String(session.authorization_id || "").toLowerCase() === String(receipt.authorization_id || "").toLowerCase()
    && session.execution_receipt_recorded === true
    && String(session.execution_receipt_action_state || "") === expectedState
  );
}

async function postExecutionReceipt(jobValue) {
  const job = globalThis.DianExecutionReadbackPolicy.normalizeJob(jobValue);
  const storedReceipt = job.execution_receipt;
  if (!storedReceipt || typeof storedReceipt !== "object") {
    const error = new Error("执行 journal 缺少可重试回执");
    error.code = "EXECUTION_RECEIPT_MISSING";
    throw error;
  }
  const receipt = {
    ...storedReceipt,
    // Upgrade an in-flight journal created by an older extension build. The
    // value is a binding field from the consumed grant, not success evidence.
    store_key: storedReceipt.store_key || job.store_key || "",
  };
  if (await executionReceiptAlreadyAcknowledged(job)) return { ok: true, recovered: true };
  try {
    const response = await bridgeFetch(`${BRIDGE_URL}/actions/execution/result`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
      body: JSON.stringify({ action_id: job.action_id, result: receipt }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload.error || "执行回执校验失败");
      error.code = payload.error_code || payload.code || "EXECUTION_RECEIPT_REJECTED";
      throw error;
    }
    return payload;
  } catch (error) {
    if (await executionReceiptAlreadyAcknowledged(job).catch(() => false)) {
      return { ok: true, recovered: true };
    }
    throw error;
  }
}

async function advanceExecutionJournalUnlocked(jobValue) {
  const policy = globalThis.DianExecutionReadbackPolicy;
  let job = policy.normalizeJob(jobValue);
  if (job.status !== "pending") return job;
  if (job.journal_state === "awaiting_consume") {
    if (
      authorizedExecutionPromises.has(String(job.authorization_id || "").toLowerCase())
      || Number(job.consume_intent_recovery_after_ms || 0) > Date.now()
    ) return job;
    const response = await bridgeFetch(`${BRIDGE_URL}/actions/preflight`, { cache: "no-store" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "无法恢复一次性授权消费状态");
    const session = payload.session;
    const sameGrant = session
      && String(session.action_id || "").toLowerCase() === job.action_id
      && String(session.authorization_id || "").toLowerCase() === String(job.authorization_id || "").toLowerCase();
    if (!sameGrant) {
      return persistExecutionReadbackJob({
        ...job,
        status_label: "一次性授权状态无法匹配，保持锁定并等待人工核对",
        last_receipt_error_code: "CONSUME_INTENT_SCOPE_MISMATCH",
        updated_at_ms: Date.now(),
      });
    }
    if (session.authorization_consumed === true || ["authorization_consumed", "manual_reconcile_required", "manual_reconcile_archived"].includes(session.state)) {
      const receipt = job.pre_dispatch_negative_receipt;
      if (!receipt || typeof receipt !== "object") {
        return persistExecutionReadbackJob({
          ...job,
          journal_state: "submission_unknown",
          status_label: "授权已消费但缺少提交前断点证据，禁止重试并转独立回读",
          updated_at_ms: Date.now(),
        });
      }
      job = await persistExecutionReadbackJob({
        ...job,
        journal_state: "awaiting_receipt",
        execution_receipt: receipt,
        status_label: "已恢复提交前断点，正在确认页面未收到执行指令",
        updated_at_ms: Date.now(),
      });
    } else if (["expired", "stopped", "invalidated", "execution_failed"].includes(String(session.state || ""))) {
      return persistExecutionReadbackJob({
        ...job,
        status: "failed",
        journal_state: "consume_not_committed",
        status_label: "一次性授权未消费且已安全终止，页面未提交",
        next_attempt_at_ms: 0,
        updated_at_ms: Date.now(),
      });
    } else {
      return persistExecutionReadbackJob({
        ...job,
        status_label: "一次性授权尚未消费；等待其过期，绝不自动重放提交",
        updated_at_ms: Date.now(),
      });
    }
  }
  if (job.journal_state === "awaiting_submission") {
    job = {
      ...job,
      journal_state: "submission_unknown",
      status_label: "页面提交结果未知，禁止重复提交；等待原店铺与账户的独立回读",
      updated_at_ms: Date.now(),
    };
    job = await persistExecutionReadbackJob(job);
  }
  if (job.journal_state !== "awaiting_receipt") return job;
  try {
    await postExecutionReceipt(job);
  } catch (error) {
    job = {
      ...job,
      status_label: job.execution_receipt?.submitted === false
        ? "页面明确未提交，等待本机 Agent 确认失败回执"
        : "页面已提交，回执暂未确认；保持锁定并等待恢复",
      last_receipt_error: String(error?.message || error).slice(0, 300),
      last_receipt_error_code: String(error?.code || "EXECUTION_RECEIPT_REJECTED"),
      updated_at_ms: Date.now(),
    };
    return persistExecutionReadbackJob(job);
  }
  if (job.execution_receipt?.submitted === false) {
    job = {
      ...job,
      status: "failed",
      journal_state: "submission_failed",
      status_label: "页面明确未提交，动作已安全终止",
      next_attempt_at_ms: 0,
      deferred_until_ms: 0,
      receipt_acknowledged_at_ms: Date.now(),
      updated_at_ms: Date.now(),
    };
  } else {
    job = {
      ...job,
      journal_state: "readback_pending",
      status_label: "执行回执已确认，等待平台独立回读",
      receipt_acknowledged_at_ms: Date.now(),
      last_receipt_error: "",
      last_receipt_error_code: "",
      updated_at_ms: Date.now(),
    };
  }
  return persistExecutionReadbackJob(job);
}

const executionJournalAdvancePromises = new Map();
function advanceExecutionJournal(jobValue, options = {}) {
  const normalized = globalThis.DianExecutionReadbackPolicy.normalizeJob(jobValue);
  const actionId = String(normalized.action_id || "");
  const existing = executionJournalAdvancePromises.get(actionId);
  if (existing) return existing;
  if (options.reloadAdmissionInherited !== true && typeof assertVersionReloadAllowsNewWork === "function") {
    assertVersionReloadAllowsNewWork("execution_journal_advance");
  }
  let tracked;
  tracked = advanceExecutionJournalUnlocked(normalized).finally(() => {
    if (executionJournalAdvancePromises.get(actionId) === tracked) {
      executionJournalAdvancePromises.delete(actionId);
    }
  });
  executionJournalAdvancePromises.set(actionId, tracked);
  return tracked;
}

function qianchuanReadbackScopeMatches(record = {}, job = {}) {
  const recordAccountKey = String(record.account_key || record.account?.key || "").trim().toLowerCase();
  const recordStoreKey = String(record.store_key || record.store?.key || record.account?.store_key || "").trim().toLowerCase();
  return Boolean(
    recordAccountKey
    && recordStoreKey
    && recordAccountKey === String(job.account_key || "").trim().toLowerCase()
    && recordStoreKey === String(job.store_key || "").trim().toLowerCase()
  );
}

function readbackTabUnavailable(message) {
  const error = new Error(message);
  error.code = "READBACK_SCOPE_TAB_UNAVAILABLE";
  return error;
}

function executionWitnessScopePayload(witness = {}) {
  return JSON.stringify({
    identity: witness.identity || null,
    promotion_mode: String(witness.promotion_mode || "unknown"),
    page_type: String(witness.page_type || "unknown"),
  });
}

async function executionWitnessScopeHash(witness = {}) {
  const identity = witness.identity && typeof witness.identity === "object" ? witness.identity : {};
  const claims = Array.isArray(identity.claims) ? identity.claims : [];
  const accountClaims = claims.filter((claim) => ["qianchuan_advertiser_id", "qianchuan_account_id"].includes(String(claim?.kind || "")));
  const pageType = String(witness.page_type || "unknown") === "qianchuan_campaigns" ? "campaigns" : String(witness.page_type || "unknown");
  if (
    identity.status !== "resolved_by_bridge"
    || !claims.length
    || identity.conflicts?.length
    || accountClaims.length !== 1
    || String(accountClaims[0]?.confidence || "") !== "high"
    || !["url_parameter", "data_attribute"].includes(String(accountClaims[0]?.evidence_source || ""))
    || !["standard", "full_domain"].includes(String(witness.promotion_mode || "unknown"))
    || !["campaigns", "qianchuan_live"].includes(pageType)
  ) {
    return "";
  }
  if (!globalThis.crypto?.subtle || typeof TextEncoder !== "function") return "";
  const digest = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(executionWitnessScopePayload(witness)));
  return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
}

async function resolveExecutionReadbackTab(job = {}, excludedTabIds = []) {
  const excluded = new Set((excludedTabIds || []).map(Number));
  const [tabs, stored] = await Promise.all([
    querySourceTabs("qianchuan"),
    chrome.storage.local.get(["recentQianchuanTab", "qianchuanSeed"]),
  ]);
  const candidates = [stored.recentQianchuanTab, stored.qianchuanSeed]
    .filter((record) => qianchuanReadbackScopeMatches(record, job));
  candidates.sort((left, right) => (
    Number(Number(right?.tab_id) === Number(job.tab_id)) - Number(Number(left?.tab_id) === Number(job.tab_id))
    || Number(right?.last_seen || right?.captured_at || 0) - Number(left?.last_seen || left?.captured_at || 0)
  ));
  const recordedTabs = candidates
    .map((record) => tabs.find((item) => Number(item?.id) === Number(record?.tab_id) && !excluded.has(Number(item?.id))))
    .filter((tab) => tab?.id && DianAgentScanPolicy.isQianchuanUrl(tab.url));
  const hasScopeHash = /^[a-f0-9]{64}$/.test(String(job.baseline_scope_hash || ""));
  const remainingTabs = hasScopeHash || job.reconstructed_from_agent === true
    ? tabs.filter((tab) => tab?.id && !excluded.has(Number(tab.id)) && DianAgentScanPolicy.isQianchuanUrl(tab.url))
    : [];
  const orderedTabs = [...recordedTabs, ...remainingTabs]
    .filter((tab, index, values) => values.findIndex((candidate) => Number(candidate.id) === Number(tab.id)) === index);
  for (const tab of orderedTabs) {
    if (!hasScopeHash) return tab;
    try {
      const witness = await readExecutionDocumentWitness(tab.id);
      const currentHash = await executionWitnessScopeHash(witness);
      if (currentHash && currentHash === job.baseline_scope_hash) return tab;
    } catch {
      // Try the next Qianchuan tab; no attempt is consumed until one tab has
      // the exact original raw identity, promotion mode and plan-page type.
    }
  }
  // Never borrow the latest tab from another account. Keep the action locked
  // and defer without consuming a verification attempt until an exact scoped
  // tab is available again.
  throw readbackTabUnavailable("未找到与原店铺和千川账户精确匹配的计划页，动作继续锁定；请打开原计划页后回读。");
}

function isReadbackScopeFailure(error) {
  const code = String(error?.code || "").toUpperCase();
  const message = String(error?.message || error || "").toUpperCase();
  return code === "READBACK_SCOPE_TAB_UNAVAILABLE"
    || code === "READBACK_SCOPE_REJECTED"
    || code === "READBACK_PLAN_NOT_VISIBLE"
    || /(SCOPE|ACCOUNT|STORE|IDENTITY).*(MISMATCH|CONFLICT|UNRESOLVED|REJECTED)/.test(`${code} ${message}`);
}

function isReadbackInfrastructureFailure(error) {
  const code = String(error?.code || "").trim().toUpperCase();
  const message = String(error?.message || error || "").trim().toUpperCase();
  const status = Number(error?.status || error?.httpStatus || 0);
  if ([408, 425, 429, 500, 502, 503, 504].includes(status)) return true;
  if (code === "REQUEST_ALREADY_IN_PROGRESS" || (status === 409 && error?.retryable === true)) return true;
  if (new Set([
    "AGENT_SESSION_REQUIRED",
    "AGENT_SESSION_INVALID",
    "AGENT_SESSION_EXPIRED",
    "EXTENSION_PAIRING_NOT_AUTHORIZED",
  ]).has(code) && [401, 403].includes(status)) return true;
  if (new Set([
    "READBACK_DOCUMENT_WITNESS_UNAVAILABLE",
    "READBACK_DOCUMENT_BASELINE_MISSING",
    "READBACK_RELOAD_UNAVAILABLE",
    "READBACK_RELOAD_TIMEOUT",
    "READBACK_CONTENT_SCRIPT_UNAVAILABLE",
    "READBACK_BRIDGE_UNAVAILABLE",
    "COLLECTION_CONTEXT_CHANGED",
    "READBACK_COLLECTION_RESPONSE_INVALID",
    "READBACK_VERIFY_RESPONSE_INVALID",
    "SNAPSHOT_ACCEPTANCE_UNCONFIRMED",
    "TARGETED_EXECUTION_ACCEPTANCE_UNCONFIRMED",
  ]).has(code)) return true;
  if (error?.bridge_reachable === false) return true;
  return error?.name === "AbortError"
    || /(?:FAILED TO FETCH|NETWORKERROR|NETWORK ERROR|LOAD FAILED|ERR_CONNECTION_|ECONNREFUSED|ECONNRESET|ETIMEDOUT|TIMEOUT)/.test(message)
    || /(?:RECEIVING END DOES NOT EXIST|MESSAGE PORT CLOSED|TAB (?:WAS )?CLOSED|FRAME (?:WAS )?REMOVED)/.test(message);
}

function isReadbackPairingFailure(error) {
  const code = String(error?.code || "").trim().toUpperCase();
  const status = Number(error?.status || error?.httpStatus || 0);
  return [401, 403].includes(status) && new Set([
    "AGENT_SESSION_REQUIRED",
    "AGENT_SESSION_INVALID",
    "AGENT_SESSION_EXPIRED",
    "EXTENSION_PAIRING_NOT_AUTHORIZED",
  ]).has(code);
}

async function verifyExecutionAction(actionId, readbackToken) {
  const response = await bridgeFetch(`${BRIDGE_URL}/actions/execution/verify`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
    body: JSON.stringify({ action_id: actionId, readback_token: readbackToken }),
  });
  let payload;
  try {
    payload = await response.json();
  } catch (cause) {
    const error = new Error("执行后验收响应不完整；保留原回读令牌并稍后重试");
    error.code = "READBACK_VERIFY_RESPONSE_INVALID";
    error.status = Number(response.status || 0);
    error.retryable = true;
    error.cause = cause;
    throw error;
  }
  if (!response.ok) {
    const error = new Error(payload.error || "执行后验收失败");
    const payloadError = String(payload.error || "");
    error.code = String(
      payload.error_code
      || payload.code
      || (/^[A-Z][A-Z0-9_]{2,80}$/.test(payloadError) ? payloadError : "EXECUTION_READBACK_VERIFY_REJECTED"),
    );
    error.status = Number(response.status || 0);
    error.retryable = payload.retryable === true;
    throw error;
  }
  const verification = payload?.verification;
  const snapshotRef = String(verification?.snapshot_ref || "");
  if (
    !verification
    || typeof verification !== "object"
    || String(verification.action_id || "").trim().toLowerCase() !== String(actionId || "").trim().toLowerCase()
    || String(verification.readback_token || "").trim().toLowerCase() !== String(readbackToken || "").trim().toLowerCase()
    || !verification.readback
    || typeof verification.readback !== "object"
    || !snapshotRef
    || !snapshotRef.endsWith(`/${String(readbackToken || "").trim().toLowerCase()}`)
  ) {
    const error = new Error("执行后验收响应缺少动作、令牌或快照绑定；保留原回读令牌并稍后重试");
    error.code = "READBACK_VERIFY_RESPONSE_INVALID";
    error.status = Number(response.status || 0);
    error.retryable = true;
    throw error;
  }
  return verification;
}

async function resolveExecutionIdentity(message, sender) {
  const snapshot = prepareScanSessionSnapshot("qianchuan", message?.data, sender);
  const response = await bridgeFetch(`${BRIDGE_URL}/actions/execution/identity-resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
    body: JSON.stringify({
      action_id: String(message?.action_id || ""),
      authorization_id: String(message?.authorization_id || ""),
      data: snapshot,
      execution_request: message?.execution_request && typeof message.execution_request === "object"
        ? message.execution_request
        : {},
    }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || "执行页面身份解析失败");
  return payload;
}

async function readExecutionDocumentWitness(tabId) {
  const response = await chrome.tabs.sendMessage(tabId, { type: "qianchuan-document-witness" });
  const witness = response?.witness;
  const documentId = String(witness?.document_instance_id || "").trim();
  if (!response?.ok || !/^[A-Za-z0-9_-]{16,128}$/.test(documentId)) {
    const error = new Error("千川页面未返回可验证的文档实例，动作继续锁定");
    error.code = "READBACK_DOCUMENT_WITNESS_UNAVAILABLE";
    throw error;
  }
  return { ...witness, document_instance_id: documentId };
}

async function reloadExecutionReadbackTab(tab, job = {}) {
  const tabId = Number(tab?.id);
  if (!Number.isInteger(tabId) || tabId <= 0 || typeof chrome.tabs.reload !== "function") {
    const error = new Error("浏览器不支持独立刷新回读，动作继续锁定");
    error.code = "READBACK_RELOAD_UNAVAILABLE";
    throw error;
  }
  // Every attempt establishes its own pre-reload baseline. Reusing the
  // execution-time document B would let a later attempt accept the previous
  // attempt's document C before the requested C→D reload actually begins.
  let baselineWitness = await readExecutionDocumentWitness(tabId);
  if (!/^[A-Za-z0-9_-]{16,128}$/.test(String(baselineWitness.document_instance_id || ""))) {
    const error = new Error("缺少执行前文档基线，不能将当前 DOM 当作独立回读");
    error.code = "READBACK_DOCUMENT_BASELINE_MISSING";
    throw error;
  }
  let expectedScopeHash = String(job.baseline_scope_hash || "");
  if (typeof executionWitnessScopeHash === "function") {
    const readinessDeadline = Date.now() + 5_000;
    let baselineScopeHash = await executionWitnessScopeHash(baselineWitness);
    while (!baselineScopeHash && Date.now() < readinessDeadline) {
      await sleep(250);
      baselineWitness = await readExecutionDocumentWitness(tabId);
      baselineScopeHash = await executionWitnessScopeHash(baselineWitness);
    }
    if (!baselineScopeHash) {
      const error = new Error("千川页面尚未显示可验证的当前账号、投放模式和计划类型；动作继续锁定");
      error.code = "READBACK_SCOPE_TAB_UNAVAILABLE";
      throw error;
    }
    if (/^[a-f0-9]{64}$/.test(expectedScopeHash) && baselineScopeHash !== expectedScopeHash) {
      const error = new Error("刷新前页面已不属于原执行账号、模式或计划类型；本次回读不计次数");
      error.code = "READBACK_SCOPE_REJECTED";
      throw error;
    }
    if (!/^[a-f0-9]{64}$/.test(expectedScopeHash)) expectedScopeHash = baselineScopeHash;
  }
  const attemptStartedAtMs = Date.now();
  await chrome.tabs.reload(tabId, { bypassCache: true });
  const deadline = Date.now() + 20_000;
  let lastMismatchedScopeHash = "";
  let consecutiveScopeMismatches = 0;
  while (Date.now() < deadline) {
    const current = await chrome.tabs.get(tabId);
    if (current?.status === "complete" && DianAgentScanPolicy.isQianchuanUrl(current.url)) {
      try {
        const witness = await readExecutionDocumentWitness(tabId);
        const navigationStartedAtMs = Number(witness.navigation_started_at_ms || 0);
        if (
          witness.document_instance_id !== baselineWitness.document_instance_id
          && navigationStartedAtMs >= attemptStartedAtMs
          && navigationStartedAtMs > Number(job.started_at_ms || 0)
        ) {
          if (/^[a-f0-9]{64}$/.test(expectedScopeHash)) {
            const reloadedScopeHash = await executionWitnessScopeHash(witness);
            if (!reloadedScopeHash) {
              // Browser status=complete can precede React identity/table
              // hydration. Unknown skeleton state is not a mismatch.
              await sleep(250);
              continue;
            }
            if (reloadedScopeHash !== expectedScopeHash) {
              consecutiveScopeMismatches = reloadedScopeHash === lastMismatchedScopeHash
                ? consecutiveScopeMismatches + 1
                : 1;
              lastMismatchedScopeHash = reloadedScopeHash;
            } else {
              consecutiveScopeMismatches = 0;
              lastMismatchedScopeHash = "";
            }
            if (consecutiveScopeMismatches >= 2) {
              const error = new Error("硬刷新后的千川账户、投放模式或计划页面已变化；本次回读不计次数并继续锁定动作");
              error.code = "READBACK_SCOPE_REJECTED";
              throw error;
            }
            if (reloadedScopeHash !== expectedScopeHash) {
              await sleep(250);
              continue;
            }
          }
          return { ...current, readback_document_witness: witness };
        }
      } catch (error) {
        if (error?.code === "READBACK_SCOPE_REJECTED") throw error;
        // The old document may still be answering while reload is beginning,
        // or the new content script may not have initialized yet. Keep polling.
      }
    }
    await sleep(250);
  }
  const error = new Error("千川页面刷新后未在安全时间内完成加载，动作继续锁定");
  error.code = "READBACK_RELOAD_TIMEOUT";
  throw error;
}

async function runExecutionReadbackAttempt(jobValue) {
  const policy = globalThis.DianExecutionReadbackPolicy;
  let job = policy.normalizeJob(jobValue);
  if (job.status !== "pending") return job;
  let collected = false;
  let verification = null;
  let errorMessage = "";
  let errorCode = "";
  try {
    const authorizationId = String(job.authorization_id || "").trim().toLowerCase();
    if (!/^[a-f0-9]{32}$/.test(authorizationId)) {
      throw new Error("执行回读任务缺少已消费授权编号，动作继续锁定");
    }
    const storedReadbackToken = String(job.active_readback_token || "").trim().toLowerCase();
    const reusingReadbackToken = /^[a-f0-9]{64}$/.test(storedReadbackToken);
    const readbackToken = reusingReadbackToken ? storedReadbackToken : createExecutionReadbackToken();
    if (!reusingReadbackToken) {
      job = await persistExecutionReadbackJob({
        ...job,
        active_readback_token: readbackToken,
        active_readback_phase: "snapshot_pending",
        active_readback_started_at_ms: Date.now(),
        updated_at_ms: Date.now(),
      });
    } else {
      // A previous worker may have committed the immutable snapshot or even the
      // verification before either the push response or the phase update was
      // delivered. Probe the same token first in both persisted phases. A 200
      // without readback evidence means the token was only journaled locally;
      // collection then continues below without consuming an attempt.
      try {
        const resumedVerification = await verifyExecutionAction(job.action_id, readbackToken);
        if (resumedVerification?.readback && String(resumedVerification?.snapshot_ref || "")) {
          verification = resumedVerification;
          collected = true;
        }
      } catch (resumeError) {
        if (isReadbackInfrastructureFailure(resumeError)) throw resumeError;
        // HTTP 400 can mean the token was journaled before the snapshot reached
        // the Agent. Continue the same transaction and collect it below.
        if (String(resumeError?.code || "") !== "EXECUTION_READBACK_SNAPSHOT_NOT_FOUND") throw resumeError;
      }
    }
    const executionContext = {
      purpose: "execution_readback",
      action_id: job.action_id,
      authorization_id: authorizationId,
      readback_token: readbackToken,
    };
    if (verification) {
      const completedJob = policy.recordAttempt({
        ...job,
        active_readback_token: "",
        active_readback_phase: "",
        active_readback_started_at_ms: 0,
      }, { collected, verification, error: "" }, Date.now());
      await persistExecutionReadbackJob(completedJob);
      return completedJob;
    }
    const excludedTabIds = [];
    while (excludedTabIds.length < 8) {
      let scopedTab = null;
      try {
        scopedTab = await resolveExecutionReadbackTab(job, excludedTabIds);
        // A click can optimistically update the current DOM even when the platform
        // later rejects the write. Every verification attempt must therefore load
        // a new document before collecting evidence.
        const tab = await reloadExecutionReadbackTab(scopedTab, job);
        const reloadedWitness = tab.readback_document_witness || {};
        const canonicalPageType = (value) => String(value || "unknown") === "qianchuan_campaigns"
          ? "campaigns"
          : String(value || "unknown");
        const expectedMode = String(job.baseline_promotion_mode || "unknown");
        const expectedPageType = canonicalPageType(job.baseline_page_type);
        if (
          (expectedMode !== "unknown" && expectedMode !== String(reloadedWitness.promotion_mode || "unknown"))
          || (expectedPageType !== "unknown" && expectedPageType !== canonicalPageType(reloadedWitness.page_type))
        ) {
          const scopeError = new Error("硬刷新后的投放模式或计划页面与原执行动作不一致；本次回读不计次数");
          scopeError.code = "READBACK_SCOPE_REJECTED";
          throw scopeError;
        }
        const attemptNumber = Number(job.next_attempt_index || 0) + 1;
        const response = await collectFromTab(
          "qianchuan",
          tab,
          `full-scan-list-post-execution-readback-${attemptNumber}`,
          { deferPush: true, targetPlanId: job.plan_id },
        );
        if (!response?.ok) {
          const collectionError = new Error(response?.error || "计划页回读未返回有效数据");
          collectionError.code = String(response?.code || response?.error_code || "READBACK_COLLECTION_REJECTED");
          throw collectionError;
        }
        if (!response.snapshot || typeof response.snapshot !== "object" || Array.isArray(response.snapshot)) {
          const collectionError = new Error("计划页回读缺少可验证快照，未保存");
          collectionError.code = "READBACK_COLLECTION_RESPONSE_INVALID";
          collectionError.retryable = true;
          throw collectionError;
        }
        if (response.snapshot.quality?.target_plan_found !== true) {
          const planError = new Error("硬刷新后前 5 页未定位到原计划；请在官方后台搜索并打开该计划后再回读，本次不计次数");
          planError.code = "READBACK_PLAN_NOT_VISIBLE";
          throw planError;
        }
        // Execution verification is not a full-scan page. Supplying a scan
        // run_id here would make the backend reject it as a stale巡店 lease.
        const bridgeResult = await storeAndPush("qianchuan", response.snapshot, {
          expectedStoreKey: job.store_key,
          expectedAccountKey: job.account_key,
          executionContext,
        });
        if (!bridgeResult?.ok) {
          const scopeError = new Error(bridgeResult?.error || "回读快照未通过店铺与账户范围校验");
          scopeError.code = bridgeResult?.error_code || "READBACK_SCOPE_REJECTED";
          scopeError.status = Number(bridgeResult?.status || 0);
          scopeError.bridge_reachable = bridgeResult?.bridge_reachable;
          throw scopeError;
        }
        collected = true;
        job = await persistExecutionReadbackJob({
          ...job,
          active_readback_token: readbackToken,
          active_readback_phase: "verification_pending",
          updated_at_ms: Date.now(),
        });
        verification = await verifyExecutionAction(job.action_id, readbackToken);
        break;
      } catch (candidateError) {
        if (scopedTab?.id && isReadbackScopeFailure(candidateError)) {
          excludedTabIds.push(Number(scopedTab.id));
          continue;
        }
        throw candidateError;
      }
    }
    if (!collected && !verification) throw readbackTabUnavailable("没有标签页通过原店铺与账户校验，动作继续锁定；请打开原计划页后回读。");
  } catch (error) {
    // A business verification attempt exists only after the Agent returned a
    // structured verification result from a new independent snapshot. Page
    // loading, content-script, bridge and transport failures are availability
    // events; consuming one of four evidence attempts for them can strand an
    // otherwise safe action in manual reconciliation.
    if (isReadbackScopeFailure(error) || isReadbackInfrastructureFailure(error)) {
      const infrastructureFailureCount = Math.max(0, Number(job.infrastructure_failure_count) || 0) + 1;
      const pairingFailure = isReadbackPairingFailure(error);
      const deferredJob = {
        ...policy.deferAttempt(job, {
        delay_ms: isReadbackScopeFailure(error)
          ? 60_000
          : Math.min(300_000, 15_000 * (2 ** Math.min(4, infrastructureFailureCount - 1))),
        error: error.message || String(error),
        error_code: error.code,
        status_label: pairingFailure
          ? "本地 Agent 配对或会话需要修复；原回读令牌已保留，修复后自动续验"
          : isReadbackScopeFailure(error)
          ? "等待重新打开原店铺、账户与计划页面后安全回读"
          : "页面或本地 Agent 暂未就绪，稍后自动重试；不消耗业务验收次数",
      }, Date.now()),
        infrastructure_failure_count: infrastructureFailureCount,
      };
      await persistExecutionReadbackJob(deferredJob);
      return deferredJob;
    }
    errorMessage = error?.message || String(error);
    errorCode = String(error?.code || "EXECUTION_READBACK_REJECTED");
  }
  const nextJob = policy.recordAttempt({
    ...job,
    active_readback_token: "",
    active_readback_phase: "",
    active_readback_started_at_ms: 0,
  }, { collected, verification, error: errorMessage, error_code: errorCode }, Date.now());
  await persistExecutionReadbackJob(nextJob);
  return nextJob;
}

async function runExecutionReadbackJobUnlocked(jobValue, options = {}) {
  const policy = globalThis.DianExecutionReadbackPolicy;
  let job = policy.normalizeJob(jobValue);
  while (job.status === "pending") {
    const next = policy.nextAttempt(job, Date.now());
    if (next.terminal) break;
    if (next.wait_ms > 0) {
      if (options.waitForSchedule === false) break;
      await sleep(next.wait_ms);
    }
    const previousAttemptAt = Number(job.attempts?.[job.attempts.length - 1]?.attempted_at_ms || 0);
    const spacingMs = Math.max(0, 1_000 - (Date.now() - previousAttemptAt));
    if (spacingMs) await sleep(spacingMs);
    job = await runExecutionReadbackAttempt(job);
    if (Number(job.deferred_until_ms || 0) > Date.now()) break;
    if (options.singleAttempt === true) break;
  }
  return job;
}

const executionReadbackRunPromises = new Map();
function runExecutionReadbackJob(jobValue, options = {}) {
  const normalized = globalThis.DianExecutionReadbackPolicy.normalizeJob(jobValue);
  const actionId = String(normalized.action_id || "");
  const existing = executionReadbackRunPromises.get(actionId);
  if (existing) return existing;
  if (options.reloadAdmissionInherited !== true && typeof assertVersionReloadAllowsNewWork === "function") {
    assertVersionReloadAllowsNewWork("execution_readback_run");
  }
  let tracked;
  tracked = runExecutionReadbackJobUnlocked(normalized, options).finally(() => {
    if (executionReadbackRunPromises.get(actionId) === tracked) {
      executionReadbackRunPromises.delete(actionId);
    }
  });
  executionReadbackRunPromises.set(actionId, tracked);
  return tracked;
}

async function runManualExecutionReadbackUnlocked(actionIdValue) {
  const actionId = String(actionIdValue || "").trim().toLowerCase();
  if (!/^[a-f0-9]{24}$/.test(actionId)) throw new Error("人工回读缺少有效动作编号");
  const inFlight = executionReadbackRunPromises.get(actionId);
  if (inFlight) await inFlight.catch(() => undefined);
  const jobs = await loadExecutionReadbackJobs();
  let storedJob = jobs[actionId];
  if (!storedJob || typeof storedJob !== "object") {
    const response = await bridgeFetch(`${BRIDGE_URL}/actions/execution/readback-job?action_id=${encodeURIComponent(actionId)}`, {
      cache: "no-store",
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || !payload.job || typeof payload.job !== "object") {
      throw new Error(payload.message || payload.error || "本地 Agent 无法恢复该动作的只读回读任务；原动作继续锁定");
    }
    storedJob = await persistExecutionReadbackJob({
      ...payload.job,
      reconstructed_from_agent: true,
      write_enabled: false,
      execution_enabled: false,
    }, { allowTerminalReopen: true });
  }
  let job = globalThis.DianExecutionReadbackPolicy.reopenManualAttempt(storedJob, Date.now());
  job = await persistExecutionReadbackJob(job, { allowTerminalReopen: true });
  if (["verified", "failed"].includes(job.status)) return job;
  return runExecutionReadbackJob(job, {
    waitForSchedule: false,
    singleAttempt: true,
    reloadAdmissionInherited: true,
  });
}

const manualExecutionReadbackPromises = new Map();
function runManualExecutionReadback(actionIdValue) {
  const actionId = String(actionIdValue || "").trim().toLowerCase();
  const existing = manualExecutionReadbackPromises.get(actionId);
  if (existing) return existing;
  let tracked;
  tracked = (async () => {
    if (typeof ensureVersionReloadStateHydrated === "function") {
      await ensureVersionReloadStateHydrated();
    }
    if (typeof assertVersionReloadAllowsNewWork === "function") {
      assertVersionReloadAllowsNewWork("manual_execution_readback");
    }
    return runManualExecutionReadbackUnlocked(actionId);
  })().finally(() => {
    if (manualExecutionReadbackPromises.get(actionId) === tracked) {
      manualExecutionReadbackPromises.delete(actionId);
    }
  });
  manualExecutionReadbackPromises.set(actionId, tracked);
  return tracked;
}

let executionReadbackRecoveryPromise = null;
async function recoverExecutionReadbackJobs(reason = "recovery") {
  if (executionReadbackRecoveryPromise) return executionReadbackRecoveryPromise;
  if (typeof ensureVersionReloadStateHydrated === "function") {
    await ensureVersionReloadStateHydrated();
  }
  if (executionReadbackRecoveryPromise) return executionReadbackRecoveryPromise;
  if (typeof assertVersionReloadAllowsNewWork === "function") {
    assertVersionReloadAllowsNewWork("execution_readback_recovery");
  }
  executionReadbackRecoveryPromise = (async () => {
    const policy = globalThis.DianExecutionReadbackPolicy;
    let jobs = await loadExecutionReadbackJobs();
    let reconstructionProbeSucceeded = false;
    let backendRecoverableReadbackCount = 0;
    try {
      const preflightResponse = await bridgeFetch(`${BRIDGE_URL}/actions/preflight`, { cache: "no-store" });
      const preflight = await preflightResponse.json().catch(() => ({}));
      if (!preflightResponse.ok) throw new Error(preflight.error || `HTTP ${preflightResponse.status}`);
      reconstructionProbeSucceeded = true;
      const actionId = String(preflight?.session?.action_id || "").trim().toLowerCase();
      const authorizationId = String(preflight?.session?.authorization_id || "").trim().toLowerCase();
      const backendState = String(preflight?.state || "");
      const backendActionState = String(preflight?.session?.execution_receipt_action_state || "");
      const localJob = jobs[actionId] && typeof jobs[actionId] === "object"
        ? policy.normalizeJob(jobs[actionId])
        : null;
      const exactLocalGrant = Boolean(
        localJob
        && /^[a-f0-9]{24}$/.test(actionId)
        && /^[a-f0-9]{32}$/.test(authorizationId)
        && String(localJob.authorization_id || "").trim().toLowerCase() === authorizationId
      );
      const terminalStatus = backendState === "completed" && backendActionState === "verified"
        ? "verified"
        : backendState === "execution_failed" && backendActionState === "failed"
          ? "failed"
          : "";
      if (exactLocalGrant && terminalStatus && !["verified", "failed"].includes(localJob.status)) {
        await persistExecutionReadbackJob({
          ...localJob,
          status: terminalStatus,
          status_label: terminalStatus === "verified"
            ? "已与本地 Agent 终态对账：平台回读确认生效"
            : "已与本地 Agent 终态对账：页面明确未提交",
          journal_state: terminalStatus === "verified" ? "readback_verified" : "submission_failed",
          active_readback_token: "",
          active_readback_phase: "",
          active_readback_started_at_ms: 0,
          next_attempt_at_ms: 0,
          deferred_until_ms: 0,
          last_verification: {
            state: terminalStatus,
            verified: terminalStatus === "verified",
            safe_to_retry: terminalStatus === "failed",
            reconciled_from_agent: true,
          },
          reconciled_from_agent_at_ms: Date.now(),
          updated_at_ms: Date.now(),
        });
        jobs = await loadExecutionReadbackJobs();
      }
      const recoverableActionIds = new Set();
      if (
        ["authorization_consumed", "manual_reconcile_required", "manual_reconcile_archived"]
          .includes(String(preflight?.state || ""))
        && /^[a-f0-9]{24}$/.test(actionId)
      ) recoverableActionIds.add(actionId);
      for (const archivedActionId of (Array.isArray(preflight?.recoverable_readback_action_ids)
        ? preflight.recoverable_readback_action_ids
        : [])) {
        const normalizedActionId = String(archivedActionId || "").trim().toLowerCase();
        if (/^[a-f0-9]{24}$/.test(normalizedActionId)) recoverableActionIds.add(normalizedActionId);
      }
      backendRecoverableReadbackCount = recoverableActionIds.size;
      for (const recoverableActionId of recoverableActionIds) {
        if (jobs[recoverableActionId] && typeof jobs[recoverableActionId] === "object") continue;
        const recoveryResponse = await bridgeFetch(
          `${BRIDGE_URL}/actions/execution/readback-job?action_id=${encodeURIComponent(recoverableActionId)}`,
          { cache: "no-store" },
        );
        const recovery = await recoveryResponse.json().catch(() => ({}));
        if (!recoveryResponse.ok || !recovery.job || typeof recovery.job !== "object") {
          // The Agent explicitly reported a recoverable consumed action. Keep
          // the periodic alarm alive until its exact read-only job is rebuilt.
          reconstructionProbeSucceeded = false;
          continue;
        }
        await persistExecutionReadbackJob({
          ...recovery.job,
          reconstructed_from_agent: true,
          write_enabled: false,
          execution_enabled: false,
          status_label: recovery.job.manual_reconcile_archived === true
            ? "已从归档账本恢复只读验收；同一计划永久禁止自动重投"
            : "扩展本地记录缺失，已从 Agent 自动恢复只读验收；绝不会重复提交",
          updated_at_ms: Date.now(),
        });
        jobs = await loadExecutionReadbackJobs();
      }
    } catch {
      // Agent or authenticated session may still be starting. Keep any local
      // jobs and let the recovery alarm retry; never synthesize a submit job.
      reconstructionProbeSucceeded = false;
    }
    const pending = Object.values(jobs)
      .map(policy.normalizeJob)
      .filter((job) => job.status === "pending")
      .sort((left, right) => Number(left.next_attempt_at_ms || 0) - Number(right.next_attempt_at_ms || 0));
    let processed = 0;
    for (const pendingJob of pending) {
      let job = await advanceExecutionJournal(pendingJob, { reloadAdmissionInherited: true });
      if (job.journal_state !== pendingJob.journal_state || job.status !== pendingJob.status) processed += 1;
      if (job.status !== "pending" || ["awaiting_consume", "awaiting_receipt"].includes(job.journal_state)) continue;
      const next = policy.nextAttempt(job, Date.now());
      if (!next.due) continue;
      await runExecutionReadbackJob(job, {
        waitForSchedule: false,
        singleAttempt: true,
        reloadAdmissionInherited: true,
      });
      processed += 1;
    }
    if (!pending.length && reconstructionProbeSucceeded && backendRecoverableReadbackCount === 0) {
      await chrome.alarms.clear(EXECUTION_READBACK_ALARM);
    } else {
      // Storage may have been cleared while the Agent is still starting. Keep a
      // periodic probe alive until the backend can prove no consumed or
      // manual-reconcile action needs reconstruction. Re-create it on every
      // worker start because browser alarm persistence is not guaranteed.
      chrome.alarms.create(EXECUTION_READBACK_ALARM, { delayInMinutes: 1, periodInMinutes: 1 });
    }
    return { ok: true, reason, pending: pending.length, processed, reconstruction_probe_succeeded: reconstructionProbeSucceeded };
  })().finally(() => {
    executionReadbackRecoveryPromise = null;
  });
  return executionReadbackRecoveryPromise;
}

async function runAuthorizedExecutionUnlocked(authorizationId) {
  const existingJobs = await loadExecutionReadbackJobs();
  const existingJob = Object.values(existingJobs).find((item) => (
    String(item?.authorization_id || "").toLowerCase() === String(authorizationId || "").toLowerCase()
  ));
  if (existingJob) {
    const recovered = await advanceExecutionJournal(existingJob, { reloadAdmissionInherited: true });
    if (recovered.status === "pending" && !["awaiting_consume", "awaiting_receipt"].includes(recovered.journal_state)) {
      return {
        ok: false,
        submitted: null,
        error_code: "EXECUTION_ALREADY_DISPATCHED",
        error: "该一次性授权已有持久执行记录；禁止再次提交，已继续原回读任务。",
        readback_job: await runExecutionReadbackJob(recovered, {
          waitForSchedule: false,
          singleAttempt: true,
          reloadAdmissionInherited: true,
        }),
      };
    }
    return {
      ok: false,
      submitted: null,
      error_code: "EXECUTION_ALREADY_RECORDED",
      error: "该一次性授权已处理；不会重复提交。",
      readback_job: recovered,
    };
  }
  const previewResponse = await bridgeFetch(`${BRIDGE_URL}/actions/preflight/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
    body: JSON.stringify({ authorization_id: authorizationId }),
  });
  const preview = await previewResponse.json();
  if (!previewResponse.ok) throw new Error(preview.error || "执行授权预检失败");
  let request = preview.preview?.execution_request;
  const actionId = String(preview.preview?.action_id || "").toLowerCase();
  if (!request || !/^[a-f0-9]{24}$/.test(actionId)) throw new Error("授权预检缺少执行参数或动作编号");
  const [tabs, stored] = await Promise.all([
    querySourceTabs("qianchuan"),
    chrome.storage.local.get(["recentQianchuanTab", "qianchuanSeed"]),
  ]);
  const scopeJob = {
    store_key: request.store_key || request.promotion_context?.account_scope?.store_id,
    account_key: request.account_key,
  };
  const scopedTab = [stored.recentQianchuanTab, stored.qianchuanSeed]
    .filter((record) => qianchuanReadbackScopeMatches(record, scopeJob))
    .map((record) => tabs.find((tab) => Number(tab?.id) === Number(record?.tab_id)))
    .find((tab) => tab?.id && DianAgentScanPolicy.isQianchuanUrl(tab.url));
  const selection = scopedTab
    ? { tab: scopedTab, matchedBy: "verified-scope" }
    : DianAgentScanPolicy.selectQianchuanSyncTab(
      tabs,
      null,
      Number(stored.recentQianchuanTab?.tab_id),
      Number(stored.qianchuanSeed?.tab_id),
    );
  if (!selection.tab?.id) throw new Error("未找到千川标签页，请打开对应计划列表");
  await inspectPlatformPage(selection.tab.id, "qianchuan");
  selection.tab = await reloadExecutionReadbackTab(selection.tab, {
    started_at_ms: Number(preview.preview?.authorized_at_ms || 0),
  });
  const finalCandidate = await collectFromTab(
    "qianchuan",
    selection.tab,
    "full-scan-list-final-preconsume-hard-reread",
    { deferPush: true, targetPlanId: request.plan_id },
  );
  if (!finalCandidate?.ok || !finalCandidate.snapshot) throw new Error(finalCandidate?.error || "最终硬刷新未返回可验证计划快照");
  if (finalCandidate.snapshot.quality?.target_plan_found !== true) {
    throw new Error("最终硬刷新后前 5 页未定位到授权计划；授权停止且页面未提交，请先在千川搜索该计划后重试");
  }
  const finalReadbackToken = createExecutionReadbackToken();
  const finalBridgeResult = await storeAndPush("qianchuan", finalCandidate.snapshot, {
    expectedStoreKey: scopeJob.store_key,
    expectedAccountKey: scopeJob.account_key,
    executionContext: {
      purpose: "preconsume_baseline",
      action_id: actionId,
      authorization_id: authorizationId,
      readback_token: finalReadbackToken,
    },
  });
  if (!finalBridgeResult?.ok) {
    const error = new Error(finalBridgeResult?.error || "最终硬刷新未通过店铺与账户校验");
    error.code = finalBridgeResult?.error_code || "FINAL_REREAD_SCOPE_REJECTED";
    throw error;
  }
  const finalRereadResponse = await bridgeFetch(`${BRIDGE_URL}/actions/preflight/final-reread`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
    body: JSON.stringify({
      authorization_id: authorizationId,
      readback_token: finalReadbackToken,
    }),
  });
  const finalReread = await finalRereadResponse.json().catch(() => ({}));
  if (!finalRereadResponse.ok) throw new Error(finalReread.error || "最终硬刷新验收失败，页面未提交");
  request = finalReread.preview?.execution_request;
  const finalBaseline = finalReread.preview?.execution_baseline || {};
  if (!request || String(finalReread.preview?.action_id || "").toLowerCase() !== actionId) {
    throw new Error("最终硬刷新返回的动作绑定不一致，页面未提交");
  }
  const probe = await chrome.tabs.sendMessage(selection.tab.id, {
    type: "qianchuan-execution-probe",
    request: {
      ...request,
      action_id: actionId,
      authorization_id: authorizationId,
      execution_attempt_id: authorizationId,
    },
  });
  if (!probe?.ok || !probe?.ready) throw new Error(probe?.error || "当前千川页面尚未满足执行条件");
  if (
    String(probe.execution_witness?.document_instance_id || "") !== String(finalBaseline.document_instance_id || "")
    || Number(probe.execution_witness?.navigation_started_at_ms || 0) !== Number(finalBaseline.navigation_started_at_ms || 0)
  ) throw new Error("最终验收后页面文档又发生变化，授权停止且页面未提交");
  let readbackJob = globalThis.DianExecutionReadbackPolicy.createJob({
    action_id: actionId,
    store_key: request.promotion_context?.account_scope?.store_id,
    account_key: request.account_key,
    plan_id: request.plan_id,
    tab_id: selection.tab.id,
    window_id: selection.tab.windowId,
    started_at_ms: Date.now(),
    baseline_document_instance_id: probe?.execution_witness?.document_instance_id || "",
    baseline_navigation_started_at_ms: Number(probe?.execution_witness?.navigation_started_at_ms || 0),
    baseline_scope_hash: await executionWitnessScopeHash(probe?.execution_witness || {}),
    baseline_promotion_mode: String(probe?.execution_witness?.promotion_mode || "unknown"),
    baseline_page_type: String(probe?.execution_witness?.page_type || "unknown"),
  });
  readbackJob = await persistExecutionReadbackJob({
    ...readbackJob,
    journal_state: "awaiting_consume",
    authorization_id: authorizationId,
    consume_intent_recovery_after_ms: Date.now() + 5_000,
    operation_type: request.operation_type,
    pre_dispatch_negative_receipt: {
      authorization_id: authorizationId,
      submitted: false,
      platform_success_observed: false,
      operation_type: request.operation_type,
      store_key: request.store_key || request.promotion_context?.account_scope?.store_id || "",
      account_key: request.account_key,
      plan_id: request.plan_id,
      target_value: request.target_value,
      error: "扩展在向页面发送执行指令前中断",
      definite_not_submitted: true,
      submission_phase: "pre_mutation",
      mutation_started: false,
      click_invoked: false,
      recovery_unverified: false,
    },
    status_label: "已持久化一次性授权消费意图，页面尚未收到执行指令",
    updated_at_ms: Date.now(),
  });
  if (readbackJob.status !== "pending" || readbackJob.journal_state !== "awaiting_consume") {
    throw new Error("一次性授权消费意图未能持久化，页面不会收到执行指令");
  }
  const consumedResponse = await bridgeFetch(`${BRIDGE_URL}/actions/preflight/consume`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
    body: JSON.stringify({
      authorization_id: authorizationId,
      readback_context: {
        scope_hash: readbackJob.baseline_scope_hash,
        document_instance_id: readbackJob.baseline_document_instance_id,
        navigation_started_at_ms: readbackJob.baseline_navigation_started_at_ms,
        promotion_mode: readbackJob.baseline_promotion_mode,
        page_type: readbackJob.baseline_page_type,
      },
    }),
  });
  const consumed = await consumedResponse.json();
  if (!consumedResponse.ok) throw new Error(consumed.error || "一次性授权消费失败");
  const consumedRequest = consumed.grant?.execution_request;
  if (!consumedRequest || consumed.grant?.action_id !== actionId) throw new Error("消费后的授权参数与预检不一致");
  if (JSON.stringify(consumedRequest) !== JSON.stringify({
    ...request,
    execution_attempt_id: authorizationId,
    execute_before_ms: consumedRequest.execute_before_ms,
  })) throw new Error("消费后的执行参数与预检绑定不一致");
  Object.assign(request, consumedRequest);
  request.action_id = actionId;
  request.authorization_id = authorizationId;
  readbackJob = await persistExecutionReadbackJob({
    ...readbackJob,
    journal_state: "awaiting_submission",
    status_label: "一次性授权已消费，等待页面返回明确提交结果",
    updated_at_ms: Date.now(),
  });
  if (readbackJob.status !== "pending" || readbackJob.journal_state !== "awaiting_submission") {
    throw new Error("执行断点状态发生竞争变化，已停止向页面派发并保持动作锁定");
  }
  let result;
  try {
    result = await chrome.tabs.sendMessage(selection.tab.id, { type: "qianchuan-supervised-submit", request });
  } catch (error) {
    // A lost extension-message response does not prove the page failed before
    // clicking. Never turn transport uncertainty into a negative receipt.
    result = {
      ok: false,
      submitted: null,
      error_code: "SUBMISSION_RESPONSE_LOST",
      error: error.message || String(error),
    };
  }
  if (result?.submitted !== true && result?.submitted !== false) {
    readbackJob = await persistExecutionReadbackJob({
      ...readbackJob,
      journal_state: "submission_unknown",
      status_label: "页面提交结果未知，禁止重复提交；等待原店铺与账户的独立回读",
      submission_error: String(result?.error || "页面未返回明确提交结果").slice(0, 300),
      submission_error_code: String(result?.error_code || "SUBMISSION_RESULT_UNKNOWN"),
      updated_at_ms: Date.now(),
    });
    const unknownReadbackJob = await runExecutionReadbackJob(readbackJob, {
      waitForSchedule: true,
      reloadAdmissionInherited: true,
    });
    await chrome.tabs.update(selection.tab.id, { active: true });
    if (Number.isInteger(selection.tab.windowId)) await chrome.windows.update(selection.tab.windowId, { focused: true });
    const error = new Error(result?.error || "页面提交结果未知；已完成独立回读并保持动作锁定");
    error.code = result?.error_code || "SUBMISSION_RESULT_UNKNOWN";
    error.readback_job = unknownReadbackJob;
    throw error;
  }
  const explicitlyNotSubmitted = result.submitted === false;
  const receipt = {
    ...result,
    authorization_id: authorizationId,
    operation_type: request.operation_type,
    store_key: request.store_key || request.promotion_context?.account_scope?.store_id || "",
    account_key: request.account_key,
    // A negative receipt carries the authorized plan/target only as binding
    // fields; it is not evidence that the platform observed those values.
    plan_id: explicitlyNotSubmitted ? request.plan_id : result?.plan_id,
    target_value: explicitlyNotSubmitted ? request.target_value : result?.target_value,
  };
  readbackJob = await persistExecutionReadbackJob({
    ...readbackJob,
    journal_state: "awaiting_receipt",
    execution_receipt: receipt,
    status_label: receipt.submitted === false
      ? "页面明确未提交，等待本机 Agent 确认失败回执"
      : "页面已提交，等待本机 Agent 确认执行回执",
    updated_at_ms: Date.now(),
  });
  readbackJob = await advanceExecutionJournal(readbackJob, { reloadAdmissionInherited: true });
  if (readbackJob.journal_state === "awaiting_receipt") {
    const error = new Error(readbackJob.last_receipt_error || "执行回执暂未确认；已保留恢复任务，将自动重试回执与回读");
    error.code = readbackJob.last_receipt_error_code || "EXECUTION_RECEIPT_PENDING";
    throw error;
  }
  if (receipt.submitted === false) throw new Error(result?.error || "页面明确未提交，动作已安全终止");
  const finalReadbackJob = await runExecutionReadbackJob(readbackJob, {
    waitForSchedule: true,
    reloadAdmissionInherited: true,
  });
  await chrome.tabs.update(selection.tab.id, { active: true });
  if (Number.isInteger(selection.tab.windowId)) await chrome.windows.update(selection.tab.windowId, { focused: true });
  if (!result?.ok) {
    const error = new Error(result?.error || "页面提交结果不确定；已完成独立回读并保持动作锁定");
    error.code = result?.error_code || "EXECUTION_SUBMISSION_UNCERTAIN";
    error.readback_job = finalReadbackJob;
    throw error;
  }
  return {
    ...result,
    readback_job: finalReadbackJob,
    verification: finalReadbackJob.last_verification || null,
  };
}

function assertVersionReloadAllowsPrivilegedExecution() {
  if (typeof versionReloadTransitionPending === "undefined" || !versionReloadTransitionPending) return;
  const error = new Error("Extension reload is pending; new privileged execution is temporarily blocked.");
  error.code = "EXTENSION_RELOAD_PENDING";
  throw error;
}

function assertVersionReloadAllowsNewWork(workKind = "background_work") {
  try {
    assertVersionReloadAllowsPrivilegedExecution();
  } catch (error) {
    error.work_kind = String(workKind || "background_work");
    throw error;
  }
}

async function runReloadTrackedOperation(workKind, operation, options = {}) {
  if (typeof operation !== "function") throw new TypeError("reload-tracked operation must be callable");
  await ensureVersionReloadStateHydrated();
  // A page-data reply may be nested inside an already tracked manual sync.
  // The parent already forces reconciliation to defer, so inherit only that
  // live admission; an independent first operation must pass the latch.
  const allowInheritedAdmission = options.allowInheritedAdmission !== false;
  if (!allowInheritedAdmission || activeReloadTrackedOperations <= 0) {
    assertVersionReloadAllowsNewWork(workKind);
  }
  activeReloadTrackedOperations += 1;
  try {
    return await operation();
  } finally {
    activeReloadTrackedOperations = Math.max(0, activeReloadTrackedOperations - 1);
  }
}

const authorizedExecutionPromises = new Map();
function runAuthorizedExecution(authorizationIdValue) {
  const authorizationId = String(authorizationIdValue || "").trim().toLowerCase();
  const existing = authorizedExecutionPromises.get(authorizationId);
  if (existing) return existing;
  assertVersionReloadAllowsNewWork("authorized_execution");
  let tracked;
  tracked = runAuthorizedExecutionUnlocked(authorizationId).finally(() => {
    if (authorizedExecutionPromises.get(authorizationId) === tracked) authorizedExecutionPromises.delete(authorizationId);
  });
  authorizedExecutionPromises.set(authorizationId, tracked);
  return tracked;
}

async function storeAndPush(source, snapshot, options = {}) {
  if (!SOURCE_PATTERNS[source] || !snapshot || typeof snapshot !== "object") {
    throw new Error("无效的数据快照");
  }

  const pageType = String(snapshot.page_type || "unknown").replace(/[^a-z0-9_-]/gi, "_");
  const capturedAt = Number(snapshot.captured_at || snapshot.timestamp || Date.now());
  const executionContext = options.executionContext && typeof options.executionContext === "object"
    ? options.executionContext
    : {};
  const targetedExecutionPurpose = ["execution_readback", "preconsume_baseline"]
    .includes(String(executionContext.purpose || ""));
  const targetedExecutionRequest = targetedExecutionPurpose
    && /^[a-f0-9]{24}$/i.test(String(executionContext.action_id || ""))
    && /^[a-f0-9]{32}$/i.test(String(executionContext.authorization_id || ""))
    && /^[a-f0-9]{64}$/i.test(String(executionContext.readback_token || ""));
  const forensicIdentityConflict = snapshotHasIdentityConflict(snapshot, source);
  let bridgeResult;
  let bridgeReachable = false;

  // Push first. A browser-cache quota error must never prevent the core sync.
  try {
    const response = await fetchWithTimeout(`${BRIDGE_URL}/push`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Dian-Agent": "2",
      },
      body: JSON.stringify({
        source,
        data: snapshot,
        expected_scope: {
          store_key: String(options.expectedStoreKey || ""),
          account_key: String(options.expectedAccountKey || ""),
        },
        scan_context: {
          run_id: String(options.runId || ""),
          page_id: String(options.pageId || ""),
          attempt_id: String(options.attemptId || ""),
        },
        execution_context: executionContext,
        ...(options.currentPageToken ? {
          current_page_context: { current_page_token: String(options.currentPageToken) },
        } : {}),
      }),
    }, 15000);
    bridgeReachable = true;
    const responseBody = await response.json().catch(() => ({}));
    if (!response.ok) {
      const failure = new Error(responseBody.error || `HTTP ${response.status}`);
      failure.code = responseBody.error_code || scanErrorCode(failure);
      failure.status = Number(response.status || 0);
      failure.response_body = responseBody;
      throw failure;
    }
    const quarantined = responseBody.quarantined === true;
    const acceptedForCurrentData = responseBody.accepted_for_current_data === true
      && responseBody.accepted === true
      && responseBody.quarantined === false;
    const acceptedTargetedExecution = responseBody.targeted_execution_snapshot === true
      && responseBody.accepted === true
      && [undefined, false].includes(responseBody.quarantined)
      && targetedExecutionRequest;
    const accepted = !forensicIdentityConflict
      && !quarantined
      && responseBody.ok !== false
      && (targetedExecutionPurpose ? acceptedTargetedExecution : acceptedForCurrentData);
    bridgeResult = {
      ...responseBody,
      ok: accepted,
      bridge_reachable: true,
      status: Number(response.status || 0),
    };
    if (!accepted) {
      bridgeResult.error_code = String(
        responseBody.error_code
        || (forensicIdentityConflict || quarantined
          ? "STORE_IDENTITY_CONFLICT"
          : targetedExecutionPurpose
            ? "TARGETED_EXECUTION_ACCEPTANCE_UNCONFIRMED"
            : "SNAPSHOT_ACCEPTANCE_UNCONFIRMED"),
      );
      bridgeResult.error = String(
        responseBody.error
        || responseBody.message
        || (forensicIdentityConflict || quarantined
          ? "The local Agent quarantined this snapshot; current data was not updated."
          : "The local Agent did not explicitly accept this snapshot for current data."),
      );
    }
  } catch (error) {
    bridgeResult = {
      ...(error?.response_body && typeof error.response_body === "object" ? error.response_body : {}),
      ok: false,
      bridge_reachable: bridgeReachable,
      status: Number(error?.status || 0),
      error: error.message || String(error),
      error_code: error.code || scanErrorCode(error),
    };
  }

  // Keep only small metadata in chrome.storage.local. Full snapshots are
  // partitioned and retained by the local bridge.
  if (bridgeResult.ok && !targetedExecutionPurpose) {
    try {
      await mutateLocalStorage(["catalog"], ({ catalog: savedCatalog }) => {
        const catalog = structuredClone(savedCatalog || {});
        catalog[source] = catalog[source] || {};
        catalog[source][pageType] = {
          captured_at: capturedAt,
          title: snapshot.title || "",
          url: snapshot.url || "",
          quality: snapshot.quality || {},
        };
        return { catalog };
      });
    } catch (error) {
      bridgeResult.cacheWarning = error.message || String(error);
    }
  }

  await updateStatus(
    "bridge",
    bridgeResult.ok ? "本地 Agent 已连接" : bridgeReachable ? "本地 Agent 已连接，页面数据未通过校验" : "本地 Agent 未启动",
    bridgeResult.ok ? "ok" : bridgeReachable ? "warning" : "error",
  );
  return bridgeResult;
}

async function updateStatus(key, message, level = "info") {
  await mutateLocalStorage(["status"], ({ status: savedStatus }) => {
    const status = structuredClone(savedStatus || {});
    status[key] = { message, level, time: Date.now() };
    return { status };
  });
}

async function updateStatusBestEffort(key, message, level = "info") {
  try {
    await updateStatus(key, message, level);
    return true;
  } catch (_) {
    // Status persistence is observability only. A storage failure must never
    // turn a successful loopback health/auth check into an offline verdict.
    return false;
  }
}

async function checkActivationStatus(runtimeVersion = "") {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), BRIDGE_LIVENESS_TIMEOUT_MS);
  try {
    const response = await fetch(`${BRIDGE_URL}/activation/status`, {
      cache: "no-store",
      headers: { "X-Dian-Agent-Extension-Version": String(runtimeVersion || "") },
      signal: controller.signal,
    });
    if (!response.ok) {
      const error = new Error(`HTTP ${response.status}`);
      error.code = "agent_activation_http_error";
      error.status = Number(response.status || 0);
      throw error;
    }
    const data = await response.json();
    if (
      !data
      || typeof data !== "object"
      || Array.isArray(data)
      || Number(data.activation_contract_version) !== 1
      || typeof data.required_extension_version !== "string"
      || !data.activation
      || typeof data.activation !== "object"
      || typeof data.activation.state !== "string"
      || typeof data.activation.reload_required !== "boolean"
    ) {
      const error = new Error("本地 Agent 激活状态合同无效");
      error.code = "agent_activation_contract_invalid";
      throw error;
    }
    return { ok: true, data };
  } catch (error) {
    return {
      ok: false,
      error_code: String(error?.code || (error?.name === "AbortError" ? "agent_activation_timeout" : "agent_activation_unreachable")),
      status: Number(error?.status || 0),
      error: error?.name === "AbortError"
        ? "本地 Agent 激活状态检查超过 3 秒"
        : error?.message || String(error),
    };
  } finally {
    clearTimeout(timeout);
  }
}

async function checkBridgeLiveness() {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), BRIDGE_LIVENESS_TIMEOUT_MS);
  try {
    // Liveness is deliberately unauthenticated. Keep it independent from the
    // extension pairing flow so a trust/config error is not shown as a dead
    // Agent process.
    const response = await fetch(`${BRIDGE_URL}/health/live`, { cache: "no-store", signal: controller.signal });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    await updateStatusBestEffort("bridge", "本地 Agent 已连接", "ok");
    return { ok: true, data };
  } catch (error) {
    await updateStatusBestEffort("bridge", "本地 Agent 未启动", "error");
    return {
      ok: false,
      error: error?.name === "AbortError"
        ? "本地 Agent 健康检查超过 3 秒"
        : error.message || String(error),
    };
  } finally {
    clearTimeout(timeout);
  }
}

async function checkBridge(options = {}) {
  const trigger = String(options.trigger || "bridge-check");
  const live = await checkBridgeLiveness();
  if (!live.ok) {
    return {
      ok: false,
      liveness_ok: false,
      authenticated: false,
      error_code: "agent_unreachable",
      error: live.error || "本地 Agent 未启动",
    };
  }

  const liveData = live.data || {};
  const manifestVersion = String(chrome.runtime.getManifest()?.version || "");
  const activationResult = await checkActivationStatus(manifestVersion);
  if (!activationResult.ok) {
    await updateStatusBestEffort("bridge", "Agent 已运行，但扩展激活状态无法核验；连接已冻结", "warning");
    return {
      ok: false,
      liveness_ok: true,
      authenticated: null,
      version_match: false,
      runtime_extension_version: manifestVersion,
      required_extension_version: "",
      installed_extension_version: "",
      reload_requested: false,
      reload_state: "activation_status_unavailable",
      reload_blocked: true,
      error_code: activationResult.error_code || "agent_activation_status_unavailable",
      error: activationResult.error || "本地 Agent 扩展激活状态不可用",
      data: liveData,
    };
  }

  const activationStatus = activationResult.data;
  const agentVersion = String(activationStatus.agent_version || liveData.version || "");
  const requiredExtensionVersion = String(activationStatus.required_extension_version || "").trim();
  const installedExtensionVersion = String(activationStatus.installed_extension_version || "").trim();
  const data = {
    ...liveData,
    agent_version: agentVersion,
    required_extension_version: requiredExtensionVersion,
    installed_extension_version: installedExtensionVersion,
    activation: activationStatus.activation,
  };

  let reconciliation;
  try {
    reconciliation = await reconcileRequiredExtensionVersion(activationStatus, trigger);
  } catch (error) {
    reconciliation = {
      version_match: false,
      required_extension_version: requiredExtensionVersion,
      installed_extension_version: installedExtensionVersion,
      runtime_extension_version: manifestVersion,
      reload_requested: false,
      reload_blocked: true,
      reload_state: "reload_guard_failed",
      reload_attempts: 0,
      guard_error: error?.message || String(error),
    };
  }
  if (reconciliation.version_match !== true) {
    const reloadMessage = reconciliation.reload_requested
      ? "检测到已部署的新扩展，正在安全重载"
      : reconciliation.reload_state === "reload_blocked"
        ? "扩展自动重载已达安全上限；请手动重新加载"
        : "Agent 与运行中的扩展版本不一致；连接已冻结";
    await updateStatusBestEffort("bridge", reloadMessage, "warning");
    return {
      ok: false,
      liveness_ok: true,
      authenticated: null,
      version_match: false,
      ...reconciliation,
      error_code: "agent_extension_version_mismatch",
      error: `Agent 要求扩展 ${requiredExtensionVersion || "未知"} / 当前运行 ${manifestVersion || "未知"}`,
      data,
    };
  }

  let authenticated = false;
  let authError = null;
  let authenticationStatus = {};
  try {
    const protectedProbe = await withTimeout(
      (async () => {
        // Authentication is a request-scoped security fact.  Probe the cheap
        // authenticated receipt instead of the aggregate /system/status view,
        // whose disk, release and snapshot diagnostics may be slow while the
        // workbench is loading business modules.
        const response = await fetchWithTimeout(`${BRIDGE_URL}/auth/status`, { cache: "no-store" }, 2500);
        return { response, payload: await responseJson(response) };
      })(),
      3000,
      "本地 Agent 认证检查超过 3 秒",
      "agent_auth_timeout",
    );
    const { response: protectedResponse, payload: protectedPayload } = protectedProbe;
    if (!protectedResponse.ok) {
      const error = new Error(protectedPayload.message || protectedPayload.error || `HTTP ${protectedResponse.status}`);
      error.code = String(protectedPayload.error || protectedPayload.error_code || "agent_auth_failed");
      error.status = Number(protectedResponse.status || 0);
      throw error;
    }
    authenticationStatus = protectedPayload && typeof protectedPayload === "object" && !Array.isArray(protectedPayload)
      ? protectedPayload
      : {};
    const authenticatedSubject = String(authenticationStatus.session_subject || "").trim().toLowerCase();
    const authenticatedExtensionVersion = String(authenticationStatus.session_extension_version || "").trim();
    if (
      authenticationStatus.authenticated !== true
      || authenticationStatus.client_kind !== "browser_extension"
      || authenticatedSubject !== String(chrome.runtime.id || "").trim().toLowerCase()
      || authenticatedExtensionVersion !== manifestVersion
    ) {
      const error = new Error("本地 Agent 返回了与当前扩展不一致的认证回执");
      error.code = "agent_auth_contract_invalid";
      throw error;
    }
    authenticated = true;
  } catch (error) {
    authError = error;
  }

  if (!authenticated) {
    await updateStatusBestEffort("bridge", "Agent 已运行，扩展未配对；请重新加载扩展或运行修复", "warning");
    return {
      ok: false,
      liveness_ok: true,
      authenticated: false,
      version_match: true,
      runtime_extension_version: manifestVersion,
      required_extension_version: requiredExtensionVersion,
      installed_extension_version: installedExtensionVersion,
      reload_requested: false,
      reload_state: "authentication_required",
      error_code: String(authError?.code || "agent_session_required"),
      error: authError?.message || "扩展无法通过本地 Agent 身份校验",
      data,
    };
  }
  const protectedRequiredVersion = String(authenticationStatus.required_extension_version || "").trim();
  if (protectedRequiredVersion && protectedRequiredVersion !== requiredExtensionVersion) {
    await updateStatusBestEffort("bridge", "Agent 激活状态与认证状态版本冲突；连接已冻结", "warning");
    return {
      ok: false,
      liveness_ok: true,
      authenticated: true,
      version_match: false,
      runtime_extension_version: manifestVersion,
      required_extension_version: requiredExtensionVersion,
      installed_extension_version: installedExtensionVersion,
      reload_requested: false,
      reload_state: "activation_contract_conflict",
      reload_blocked: true,
      error_code: "agent_extension_version_contract_conflict",
      error: "Agent 公开激活状态与认证状态返回了不同的扩展版本",
      data,
    };
  }
  await withTimeout(
    reportExtensionInstallSource("authenticated-health"),
    1500,
    "扩展来源回执暂未保存",
  ).catch(() => undefined);
  await updateStatusBestEffort("bridge", "本地 Agent 已认证连接", "ok");
  return {
    ok: true,
    liveness_ok: true,
    authenticated: true,
    version_match: true,
    runtime_extension_version: manifestVersion,
    required_extension_version: requiredExtensionVersion,
    installed_extension_version: installedExtensionVersion,
    reload_requested: false,
    reload_state: "matched",
    data,
  };
}

async function getDashboard() {
  const [stored, doudianTabs, qianchuanTabs, bridge] = await Promise.all([
    chrome.storage.local.get(["status", "catalog", "settings", "lastSyncAttempt", "lastSyncAttemptAt", "lastSuccessfulSync", "fullScan", EXECUTION_READBACK_STORAGE_KEY]),
    querySourceTabs("doudian"),
    querySourceTabs("qianchuan"),
    checkBridge(),
  ]);
  let fullScan = stored.fullScan || {
    status: "idle",
    planned_page_ids: DianAgentScanScopePolicy.fullScanPageIds({ includeAds: false }),
    total: DianAgentScanScopePolicy.fullScanPageIds({ includeAds: false }).length,
    index: 0,
    success: 0,
    failed: 0,
  };
  // MV3 workers can be reclaimed during a multi-page scan. Persist a recoverable
  // checkpoint, but never reopen platform pages merely because the dashboard
  // or browser started. The user must explicitly choose "继续巡店".
  if (["running", "interrupted"].includes(fullScan.status) && !fullScanPromise) {
    await recoverInterruptedScan("dashboard-open");
    fullScan = (await chrome.storage.local.get("fullScan")).fullScan || fullScan;
  }
  return {
    status: stored.status || {},
    catalog: stored.catalog || {},
    settings: { ...DEFAULT_SETTINGS, ...(stored.settings || {}) },
    lastSyncAttempt: stored.lastSyncAttemptAt || stored.lastSyncAttempt || null,
    lastSuccessfulSync: stored.lastSuccessfulSync || stored.lastSyncAttempt || null,
    fullScan,
    executionReadback: globalThis.DianExecutionReadbackPolicy.summarize(stored[EXECUTION_READBACK_STORAGE_KEY] || {}),
    tabs: { doudian: doudianTabs.length, qianchuan: qianchuanTabs.length },
    bridge,
  };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const messageType = String(message?.type || "");
  const platformPageMessage = TRUSTED_PLATFORM_MESSAGE_TYPES.has(messageType);
  const senderTrusted = platformPageMessage
    ? globalThis.DianExecutionSenderPolicy.isTrustedPageDataSender(sender, chrome.runtime.id, message?.source)
    : globalThis.DianExecutionSenderPolicy.isTrustedExtensionPageSender(sender, chrome.runtime.id);
  if (!senderTrusted) {
    sendResponse({
      ok: false,
      code: platformPageMessage ? "UNTRUSTED_PLATFORM_PAGE_SENDER" : "UNTRUSTED_UI_MESSAGE_SENDER",
      error: platformPageMessage
        ? "页面数据来源与声明的平台不一致，候选快照已拒绝。"
        : "该操作只能由店策扩展自己的受信页面发起。",
    });
    return false;
  }
  if (message.type === "repair-agent") {
    chrome.tabs.create({ url: chrome.runtime.getURL("welcome.html#repair-agent") })
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: error.message || "修复指引打开失败" }));
    return true;
  }
  (async () => {
    if (message.type === "runtime-context-ping") {
      sendResponse({
        ok: true,
        runtime_version: String(chrome.runtime.getManifest()?.version || ""),
        extension_id: chrome.runtime.id,
        received_at: Date.now(),
      });
      return;
    }
    if (message.type === "platform-assistant-status") {
      const source = String(message.source || "");
      const [bridge, stored] = await Promise.all([
        checkBridge({ trigger: "platform-assistant-status" }),
        chrome.storage.local.get(PAGE_SYNC_RECEIPTS_KEY),
      ]);
      const receipt = stored[PAGE_SYNC_RECEIPTS_KEY]?.[source] || {};
      const lastSyncAt = Number(receipt.last_success_at || 0);
      const lastAttemptAt = Number(receipt.last_attempt_at || 0);
      sendResponse({
        ok: true,
        agent: {
          connected: bridge?.ok === true && bridge?.authenticated === true && bridge?.version_match === true,
          liveness_ok: bridge?.liveness_ok === true,
          authenticated: bridge?.authenticated === true,
          version_match: bridge?.version_match === true,
          runtime_version: String(bridge?.runtime_extension_version || chrome.runtime.getManifest()?.version || ""),
          extension_id: chrome.runtime.id,
          message: String(bridge?.error || ""),
        },
        last_sync_at: Number.isFinite(lastSyncAt) ? lastSyncAt : 0,
        last_sync_attempt_at: Number.isFinite(lastAttemptAt) ? lastAttemptAt : 0,
        last_sync_status: String(receipt.status || ""),
        last_sync_page_type: String(receipt.page_type || ""),
        last_sync_message: String(receipt.message || ""),
        last_sync_error_code: String(receipt.error_code || ""),
      });
      return;
    }
    if (message.type === "platform-assistant-sync") {
      if (fullScanPromise) {
        sendResponse({ ok: false, code: "SCAN_BUSY", error: "全店巡检正在进行，完成后再同步当前页，避免重复写入。" });
        return;
      }
      const source = String(message.source || "");
      sendResponse({
        ok: true,
        result: await runReloadTrackedOperation("platform_assistant_sync", () => (
          syncCurrentPageWithReceipt(source, {
            targetTabId: Number(sender?.tab?.id),
            purpose: "platform-assistant",
          })
        )),
      });
      return;
    }
    if (message.type === "page-data") {
      const snapshot = prepareScanSessionSnapshot(message.source, message.data, sender);
      const expectedScope = message.expected_scope && typeof message.expected_scope === "object"
        ? message.expected_scope
        : {};
      sendResponse(await runReloadTrackedOperation("page_data_sync", () => (
        storeAndPush(message.source, snapshot, {
          expectedStoreKey: String(expectedScope.store_key || ""),
          expectedAccountKey: String(expectedScope.account_key || ""),
        })
      )));
      return;
    }
    if (message.type === "manual-sync" || message.type === "manual-poll") {
      if (fullScanPromise) {
        sendResponse({ ok: false, code: "SCAN_BUSY", error: "全店巡检正在进行，完成后再同步，避免重复采集。" });
        return;
      }
      sendResponse({
        ok: true,
        results: await runReloadTrackedOperation("manual_sync", () => syncAll("manual")),
      });
      return;
    }
    if (message.type === "sync-current-page") {
      if (fullScanPromise) {
        sendResponse({ ok: false, code: "SCAN_BUSY", error: "全店巡检正在进行，当前页无需重复同步。" });
        return;
      }
      sendResponse({
        ok: true,
        result: await runReloadTrackedOperation("current_page_sync", () => syncCurrentPageWithReceipt(
          String(message.source_only || ""),
          {
            targetTabId: Number(message.target_tab_id || 0),
            purpose: String(message.purpose || "manual"),
          },
        )),
      });
      return;
    }
    if (message.type === "sync-current-qianchuan") {
      if (fullScanPromise) {
        sendResponse({ ok: false, code: "SCAN_BUSY", error: "全店巡检正在进行，千川页将在本轮自动读取。" });
        return;
      }
      sendResponse({
        ok: true,
        result: await runReloadTrackedOperation("qianchuan_page_sync", () => (
          syncCurrentPageWithReceipt("qianchuan", {
            expectedPageTypes: message.expected_page_types,
            expectedAccountKey: message.expected_account_key,
            expectedStoreKey: message.expected_store_key,
            purpose: message.purpose,
            requireHardReload: message.require_hard_reload === true,
            sessionStartedAtMs: message.session_started_at_ms,
          })
        )),
      });
      return;
    }
    if (message.type === "resolve-execution-identity") {
      sendResponse(await runReloadTrackedOperation(
        "execution_identity_resolution",
        () => resolveExecutionIdentity(message, sender),
        { allowInheritedAdmission: false },
      ));
      return;
    }
    if (message.type === "run-authorized-execution") {
      if (!globalThis.DianExecutionSenderPolicy.isTrustedExecutionSender(sender, chrome.runtime.id)) {
        sendResponse({
          ok: false,
          code: "UNTRUSTED_EXECUTION_SENDER",
          error: "受监督执行只能由店策经营工作台发起。",
        });
        return;
      }
      await ensureVersionReloadStateHydrated();
      sendResponse({ ok: true, result: await runAuthorizedExecution(String(message.authorization_id || "")) });
      return;
    }
    if (message.type === "manual-execution-readback") {
      if (!globalThis.DianExecutionSenderPolicy.isTrustedExecutionSender(sender, chrome.runtime.id)) {
        sendResponse({ ok: false, code: "UNTRUSTED_EXECUTION_SENDER", error: "执行回读只能由店策经营工作台发起。" });
        return;
      }
      const job = await runManualExecutionReadback(String(message.action_id || ""));
      sendResponse({ ok: true, job, verification: job.last_verification || null });
      return;
    }
    if (message.type === "start-full-scan") {
      if (fullScanPromise) {
        sendResponse({ ok: true, started: false, code: "SCAN_BUSY", run_id: activeFullScanRunId, message: "巡检正在进行" });
      } else {
        await ensureVersionReloadStateHydrated();
        const operation = startFullScan("manual", Array.isArray(message.page_ids) ? message.page_ids : null, String(message.account_key || ""), String(message.store_key || ""), String(message.scan_scope || ""));
        operation.catch(() => undefined);
        sendResponse(await awaitFullScanLaunch(operation));
      }
      return;
    }
    if (message.type === "retry-failed-scan") {
      const stored = await chrome.storage.local.get("fullScan");
      const scan = stored.fullScan || {};
      const planned = Array.isArray(scan.planned_page_ids) && scan.planned_page_ids.length
        ? scan.planned_page_ids
        : DianAgentScanScopePolicy.receiptPlan({ ...scan, scope: scan.scope || "full" }).expected_page_ids;
      const recovery = buildRecoveryCheckpoint(scan, planned);
      const resumeIds = Array.isArray(recovery.resume_page_ids) ? recovery.resume_page_ids : [];
      if (fullScanPromise) sendResponse({ ok: true, started: false, code: "SCAN_BUSY", run_id: activeFullScanRunId, message: "巡检正在进行" });
      else if (!recovery.can_resume || !resumeIds.length) sendResponse({ ok: false, code: recovery.error_code || "NO_PENDING_PAGES", error: recovery.message || "没有需要重试或续跑的页面", recovery });
      else {
        await ensureVersionReloadStateHydrated();
        const operation = startFullScan(
          "retry-failed-current-account", resumeIds, String(scan.account_key || ""),
          String(scan.store_key || ""), String(scan.scope || "full"), { ...scan, planned_page_ids: planned },
        );
        operation.catch(() => undefined);
        const launch = await awaitFullScanLaunch(operation);
        sendResponse(launch.started ? { ...launch, total: resumeIds.length, recovery } : { ...launch, recovery });
      }
      return;
    }
    if (message.type === "cancel-full-scan") {
      const stored = await chrome.storage.local.get("fullScan");
      const scan = stored.fullScan || {};
      const runId = activeFullScanRunId || String(scan.run_id || "");
      if (!canCancelScan({ ...scan, run_id: runId })) {
        sendResponse({ ok: true, cancelled: false, code: "SCAN_NOT_RUNNING", run_id: runId });
        return;
      }
      if (runId) cancelledFullScanRuns.add(runId);
      if (Number.isInteger(activeFullScanTabId)) {
        clearDoudianScanContext(activeFullScanTabId);
        await chrome.tabs.remove(activeFullScanTabId).catch(() => undefined);
        activeFullScanTabId = null;
      }
      if (runId) {
        const recovery = buildRecoveryCheckpoint({ ...scan, status: "cancelled" });
        await setFullScanState({
          status: "cancelled", current: "", finished_at: Date.now(), owned_tab_id: null,
          error: "已由用户安全停止", error_code: "SCAN_CANCELLED",
          recovery,
        }, runId);
      }
      sendResponse({ ok: true, cancelled: Boolean(runId), run_id: runId });
      return;
    }
    if (message.type === "get-dashboard" || message.type === "get-status") {
      sendResponse({ ok: true, dashboard: await getDashboard() });
      return;
    }
    if (message.type === "test-bridge") {
      sendResponse(await checkBridge());
      return;
    }
    if (message.type === "update-settings") {
      const next = { ...(await getSettings()), ...(message.settings || {}) };
      next.intervalMinutes = Math.max(1, Number(next.intervalMinutes) || 5);
      next.fullScanIntervalHours = Math.max(1, Number(next.fullScanIntervalHours) || 6);
      next.operatingMode = next.operatingMode === "full" ? "full" : "sentinel";
      next.autoSync = false;
      next.autoFullScan = false;
      await chrome.storage.local.set({ settings: next });
      await configureAlarm();
      sendResponse({ ok: true, settings: next });
      return;
    }
    if (message.type === "open-platform") {
      const url = SOURCE_URLS[message.source];
      if (!url) throw new Error("未知平台");
      await chrome.tabs.create({ url });
      sendResponse({ ok: true });
      return;
    }
    if (message.type === "show-platform-assistant") {
      await chrome.storage.local.set({
        [`${PLATFORM_ASSISTANT_DISMISSED_KEY}.doudian`]: false,
        [`${PLATFORM_ASSISTANT_DISMISSED_KEY}.qianchuan`]: false,
        [PLATFORM_ASSISTANT_LEGACY_DISMISSED_KEY]: false,
      });
      const assistants = await ensureOpenPlatformAssistants();
      sendResponse({ ok: true, assistants });
      return;
    }
    if (message.type === "open-extension-manager") {
      await chrome.tabs.create({ url: "chrome://extensions/" });
      sendResponse({ ok: true });
      return;
    }
    if (message.type === "open-workbench" || message.type === "platform-assistant-open-workbench") {
      await openWorkbench(String(message.route || ""));
      sendResponse({ ok: true });
      return;
    }
    sendResponse({ ok: false, error: "未知消息" });
  })().catch((error) => sendResponse({ ok: false, code: error.code || "", error: error.message || String(error) }));
  return true;
});
