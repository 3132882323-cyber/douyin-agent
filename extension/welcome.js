const BRIDGE_URL = "http://127.0.0.1:8765";
let RUNTIME_EXTENSION_VERSION = "";
let RUNTIME_EXTENSION_STORE_MANAGED = false;
let WORKBENCH_URL = "sidepanel.html";
let bridgeAuthClient = null;
try {
  const runtimeManifest = chrome.runtime.getManifest() || {};
  RUNTIME_EXTENSION_VERSION = String(runtimeManifest.version || "").trim();
  RUNTIME_EXTENSION_STORE_MANAGED = Boolean(String(runtimeManifest.update_url || "").trim());
  WORKBENCH_URL = chrome.runtime.getURL("sidepanel.html");
  bridgeAuthClient = globalThis.DianBridgeAuth.createClient({ baseUrl: BRIDGE_URL });
} catch {
  // An already-open extension page can outlive chrome.runtime after an unpacked
  // update. Public activation fetches below use these safe cached fallbacks to
  // recover without first touching the invalidated extension context again.
}
const WELCOME_HEALTH_TIMEOUT_MS = 3000;
const WELCOME_STORES_TIMEOUT_MS = 4000;
const WELCOME_ACTIVATION_TIMEOUT_MS = 3000;
const WELCOME_VERSION_SWITCH_DELAY_MS = 800;
const WELCOME_RECHECK_DELAYS_MS = Object.freeze([3000, 10000, 30000]);
const WELCOME_RELOAD_GUARD_KEY = "dianAgentWelcomeContextReloadV1";
const WELCOME_RELOAD_GUARD_MS = 10000;
const WELCOME_RELOAD_GUARD_TTL_MS = 60000;
const WELCOME_RELOAD_RECHECK_PADDING_MS = 250;
const WELCOME_RELOAD_MAX_ATTEMPTS = 2;
let bridgeCheckInFlight = null;
let bridgeRecheckTimer = null;
let bridgeRecheckAttempts = 0;
let contextReloadRequested = false;
let guardedRecoveryTimer = null;

function timedOperation(operation, timeoutMs, label, onTimeout = () => undefined) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      onTimeout();
      const error = new Error(`${label}超时`);
      error.code = "welcome_check_timeout";
      reject(error);
    }, timeoutMs);
  });
  return Promise.race([operation, timeout]).finally(() => clearTimeout(timer));
}

async function fetchHealth() {
  const controller = new AbortController();
  return timedOperation(
    (async () => {
      let response;
      try {
        response = await fetch(`${BRIDGE_URL}/health`, { cache: "no-store", signal: controller.signal });
      } catch (error) {
        if (explicitNetworkError(error) && !error?.code) error.code = "welcome_network_error";
        throw error;
      }
      if (!response?.ok) {
        const error = new Error(`本地 Agent 健康检查返回 HTTP ${Number(response?.status || 0)}`);
        error.code = "welcome_health_http_error";
        error.status = Number(response?.status || 0);
        throw error;
      }
      let health;
      try {
        health = await response.json();
      } catch (previousError) {
        const error = new Error("本地 Agent 健康检查返回了无效 JSON");
        error.code = "welcome_health_schema_invalid";
        error.previousError = previousError;
        throw error;
      }
      if (
        !health
        || typeof health !== "object"
        || Array.isArray(health)
        || health.status !== "ok"
        || !String(health.version || "").trim()
      ) {
        const error = new Error("本地 Agent 健康检查响应格式不正确");
        error.code = "welcome_health_schema_invalid";
        throw error;
      }
      return health;
    })(),
    WELCOME_HEALTH_TIMEOUT_MS,
    "检查本地 Agent",
    () => controller.abort(),
  );
}

async function fetchStores() {
  const controller = new AbortController();
  return timedOperation(
    bridgeAuthClient.fetchJson("/stores", { signal: controller.signal }),
    WELCOME_STORES_TIMEOUT_MS,
    "读取店铺连接",
    () => controller.abort(),
  );
}

async function hasOpenDoudianTab() {
  if (typeof chrome.tabs?.query !== "function") return false;
  try {
    const tabs = await chrome.tabs.query({ url: "https://fxg.jinritemai.com/*" });
    return Array.isArray(tabs) && tabs.some((tab) => Number.isInteger(tab?.id));
  } catch {
    return false;
  }
}

function welcomeStoreState(catalog = {}, doudianTabOpen = false) {
  const stores = Array.isArray(catalog?.stores) ? catalog.stores : [];
  const storeCount = Math.max(0, Number(catalog?.store_count || stores.length || 0));
  const selectedStoreKey = String(catalog?.selected_store_key || "").trim().toLowerCase();
  const selectedStore = stores.find(
    (store) => String(store?.key || "").trim().toLowerCase() === selectedStoreKey,
  ) || null;

  if (!doudianTabOpen) {
    return {
      text: "尚未打开抖店，请先登录并保持页面打开",
      title: "Agent 已连接；当前没有可继续巡店的抖店页面",
      className: "state warn",
    };
  }
  if (!storeCount) {
    return {
      text: "抖店已打开，尚未读取经营数据",
      title: "打开工作台后点击“开始快速巡店”",
      className: "state warn",
    };
  }
  if (!selectedStoreKey || !selectedStore) {
    return {
      text: "抖店已打开，等待确认当前店铺",
      title: "开始巡店时会从当前抖店页面安全确认经营范围",
      className: "state warn",
    };
  }
  if (Math.max(0, Number(selectedStore.doudian_page_count || 0)) === 0) {
    return {
      text: "店铺已识别，等待首次巡店",
      title: "进入工作台后点击“开始快速巡店”",
      className: "state warn",
    };
  }
  if (selectedStore.doudian_fresh !== true) {
    return {
      text: "店铺已识别，经营数据需要刷新",
      title: "当前仅有历史数据；进入工作台后重新巡店",
      className: "state warn",
    };
  }
  return {
    text: "抖店已连接，可以开始巡店",
    title: "进入工作台查看数据时效并开始巡店",
    className: "state ok",
  };
}

async function fetchActivationStatus() {
  const controller = new AbortController();
  return timedOperation(
    (async () => {
      const response = await fetch(`${BRIDGE_URL}/activation/status`, {
        cache: "no-store",
        headers: { "X-Dian-Agent-Extension-Version": RUNTIME_EXTENSION_VERSION },
        signal: controller.signal,
      });
      if (!response.ok) {
        const error = new Error(`本地 Agent 激活状态返回 HTTP ${response.status}`);
        error.code = "welcome_activation_http_error";
        error.status = Number(response.status || 0);
        throw error;
      }
      const activationStatus = await response.json();
      if (
        !activationStatus
        || typeof activationStatus !== "object"
        || Array.isArray(activationStatus)
        || Number(activationStatus.activation_contract_version) !== 1
        || typeof activationStatus.required_extension_version !== "string"
        || !activationStatus.activation
        || typeof activationStatus.activation !== "object"
        || typeof activationStatus.activation.state !== "string"
        || typeof activationStatus.activation.reload_required !== "boolean"
      ) {
        const error = new Error("本地 Agent 激活状态响应格式不正确");
        error.code = "welcome_activation_schema_invalid";
        throw error;
      }
      return activationStatus;
    })(),
    WELCOME_ACTIVATION_TIMEOUT_MS,
    "核验扩展激活状态",
    () => controller.abort(),
  );
}

function renderActivationLayers(health = {}, activationStatus = {}) {
  const node = document.getElementById("activation-version-layers");
  if (!node) return;
  const agent = String(activationStatus.agent_version || health.version || "未知");
  const installed = String(activationStatus.installed_extension_version || "未确认");
  const runtime = RUNTIME_EXTENSION_VERSION || "未确认";
  node.textContent = `Agent v${agent} · 磁盘扩展 v${installed} · 浏览器运行 v${runtime}`;
  node.hidden = false;
}

function compareReleaseVersions(left, right) {
  const parse = (value) => {
    const candidate = String(value || "").trim();
    if (!/^\d+\.\d+\.\d+$/.test(candidate)) return null;
    const parts = candidate.split(".").map(Number);
    return parts.every((part) => Number.isSafeInteger(part) && part >= 0) ? parts : null;
  };
  const leftParts = parse(left);
  const rightParts = parse(right);
  if (!leftParts || !rightParts) return null;
  for (let index = 0; index < 3; index += 1) {
    if (leftParts[index] !== rightParts[index]) {
      return leftParts[index] > rightParts[index] ? 1 : -1;
    }
  }
  return 0;
}

function activationNeedsPageReload(activationStatus = {}) {
  const required = String(activationStatus.required_extension_version || "").trim();
  const installed = String(activationStatus.installed_extension_version || "").trim();
  if (!required || installed !== required) return false;
  if (
    activationStatus.activation?.state === "reload_required"
    && activationStatus.activation?.reload_required === true
  ) {
    return !RUNTIME_EXTENSION_STORE_MANAGED
      && compareReleaseVersions(required, RUNTIME_EXTENSION_VERSION) === 1;
  }
  // An extension page can survive an unpacked runtime reload while all
  // chrome.runtime APIs are already invalid. In that one narrowly proven case
  // the public Agent contract cannot receive a runtime version and reports
  // installed_unconfirmed. Reload the dead document with the same bounded
  // guard; never treat a genuine disk/Agent mismatch as reloadable.
  return activationStatus.activation?.state === "installed_unconfirmed"
    && !RUNTIME_EXTENSION_VERSION;
}

function renderRuntimeVersionMismatch(activationStatus, bridgeStatus, storeStatus) {
  const required = String(activationStatus.required_extension_version || "未知");
  const installed = String(activationStatus.installed_extension_version || "未确认");
  const runtime = RUNTIME_EXTENSION_VERSION || "未确认";
  const state = String(activationStatus.activation?.state || "version_mismatch");
  setRepairGuideVisible(false);
  bridgeStatus.className = "state warn";
  storeStatus.className = "state warn";
  bridgeStatus.textContent = `扩展正在切换到 v${installed}`;
  storeStatus.textContent = `Agent 需要 v${required}，当前浏览器仍运行 v${runtime}；正在安全载入磁盘版本`;
  storeStatus.title = `runtime_version_mismatch · ${state}`;
}

function requestBackgroundVersionReconcile() {
  try {
    chrome.runtime.sendMessage({ type: "test-bridge" }).catch(() => undefined);
  } catch {
    // The stale page can still reload itself from public activation evidence.
  }
}

function extensionContextInvalidated(error = {}) {
  const messages = [error?.message, error?.previousError?.message]
    .filter(Boolean)
    .map((value) => String(value));
  return messages.some((message) => /extension context (?:was )?invalidated/i.test(message));
}

function emptyWelcomeReloadGuard(storageAvailable = true) {
  return {
    storageAvailable,
    firstRequestedAt: 0,
    lastRequestedAt: 0,
    attempts: 0,
  };
}

function readWelcomeReloadGuard(now = Date.now()) {
  let raw;
  try {
    raw = sessionStorage.getItem(WELCOME_RELOAD_GUARD_KEY);
  } catch {
    return emptyWelcomeReloadGuard(false);
  }
  if (!raw) return emptyWelcomeReloadGuard(true);
  try {
    const decoded = JSON.parse(raw);
    const legacyTimestamp = typeof decoded === "number" ? decoded : 0;
    const firstRequestedAt = Number(
      legacyTimestamp || decoded?.first_requested_at || decoded?.last_requested_at || 0,
    );
    const lastRequestedAt = Number(legacyTimestamp || decoded?.last_requested_at || 0);
    const attempts = legacyTimestamp ? 1 : Number(decoded?.attempts || 0);
    const valid = Number.isFinite(firstRequestedAt)
      && Number.isFinite(lastRequestedAt)
      && Number.isInteger(attempts)
      && firstRequestedAt > 0
      && lastRequestedAt >= firstRequestedAt
      && lastRequestedAt <= now + 5000
      && attempts >= 1
      && attempts <= WELCOME_RELOAD_MAX_ATTEMPTS;
    if (!valid || now - firstRequestedAt > WELCOME_RELOAD_GUARD_TTL_MS) {
      return emptyWelcomeReloadGuard(true);
    }
    return { storageAvailable: true, firstRequestedAt, lastRequestedAt, attempts };
  } catch {
    return emptyWelcomeReloadGuard(true);
  }
}

function writeWelcomeReloadGuard(guard) {
  try {
    sessionStorage.setItem(WELCOME_RELOAD_GUARD_KEY, JSON.stringify({
      first_requested_at: guard.firstRequestedAt,
      last_requested_at: guard.lastRequestedAt,
      attempts: guard.attempts,
    }));
    return true;
  } catch {
    return false;
  }
}

function renderManualWelcomeReload(storeStatus, options = {}) {
  storeStatus.textContent = options.guardedText
    || "扩展自动切换未完成，请按 Ctrl+R；仍无效时在扩展管理页重新加载";
  storeStatus.title = options.guardedTitle || "自动恢复已安全尝试两次，已停止继续刷新";
  storeStatus.className = "state warn";
}

function requestGuardedWelcomeReload(storeStatus, options = {}) {
  clearBridgeRecheck();
  if (contextReloadRequested) return true;
  const now = Date.now();
  const guard = readWelcomeReloadGuard(now);
  if (!guard.storageAvailable) {
    contextReloadRequested = true;
    renderManualWelcomeReload(storeStatus, {
      ...options,
      guardedText: "浏览器无法保存自动恢复状态，请按 Ctrl+R 安全重新载入",
      guardedTitle: "自动刷新保护不可用，已停止自动重载以避免循环",
    });
    return true;
  }
  const elapsed = now - guard.lastRequestedAt;
  if (guard.attempts > 0 && elapsed < WELCOME_RELOAD_GUARD_MS) {
    contextReloadRequested = true;
    const delay = WELCOME_RELOAD_GUARD_MS - Math.max(0, elapsed)
      + WELCOME_RELOAD_RECHECK_PADDING_MS;
    storeStatus.textContent = options.waitingText || "浏览器正在完成扩展切换，稍后会自动复检，无需操作";
    storeStatus.title = `自动恢复第 ${guard.attempts} 次已触发，${Math.ceil(delay / 1000)} 秒后复检`;
    storeStatus.className = "state warn";
    guardedRecoveryTimer = setTimeout(() => {
      guardedRecoveryTimer = null;
      contextReloadRequested = false;
      checkBridge().catch(() => undefined);
    }, delay);
    return true;
  }
  if (guard.attempts >= WELCOME_RELOAD_MAX_ATTEMPTS) {
    contextReloadRequested = true;
    renderManualWelcomeReload(storeStatus, options);
    return true;
  }
  const nextGuard = {
    firstRequestedAt: guard.firstRequestedAt || now,
    lastRequestedAt: now,
    attempts: guard.attempts + 1,
  };
  if (!writeWelcomeReloadGuard(nextGuard)) {
    contextReloadRequested = true;
    renderManualWelcomeReload(storeStatus, {
      ...options,
      guardedText: "浏览器无法保存自动恢复进度，请按 Ctrl+R 安全重新载入",
      guardedTitle: "自动刷新保护写入失败，已停止自动重载以避免循环",
    });
    return true;
  }
  contextReloadRequested = true;
  storeStatus.textContent = options.loadingText || "扩展已更新，正在重新载入页面";
  storeStatus.title = options.loadingTitle || "旧扩展页面上下文已失效";
  storeStatus.className = "state warn";
  const delayMs = Number.isFinite(Number(options.delayMs))
    ? Math.max(0, Math.min(2000, Number(options.delayMs)))
    : 0;
  setTimeout(() => location.reload(), delayMs);
  return true;
}

function recoverInvalidatedExtensionContext(error, storeStatus) {
  if (!extensionContextInvalidated(error)) return false;
  return requestGuardedWelcomeReload(storeStatus, {
    guardedText: "扩展已更新，请按 Ctrl+R 重新载入此页面",
    guardedTitle: "旧扩展页面已失效，已阻止重复自动刷新",
    loadingText: "扩展已更新，正在重新载入页面",
    loadingTitle: "旧扩展页面上下文已失效",
  });
}

function clearWelcomeReloadGuard() {
  if (guardedRecoveryTimer) clearTimeout(guardedRecoveryTimer);
  guardedRecoveryTimer = null;
  try {
    sessionStorage.removeItem(WELCOME_RELOAD_GUARD_KEY);
  } catch {
    // The successful authenticated result is authoritative even if storage is unavailable.
  }
  contextReloadRequested = false;
}

function clearBridgeRecheck() {
  if (bridgeRecheckTimer) clearTimeout(bridgeRecheckTimer);
  bridgeRecheckTimer = null;
}

function scheduleBridgeRecheck() {
  clearBridgeRecheck();
  if (bridgeRecheckAttempts >= WELCOME_RECHECK_DELAYS_MS.length) return;
  const delay = WELCOME_RECHECK_DELAYS_MS[bridgeRecheckAttempts];
  bridgeRecheckAttempts += 1;
  bridgeRecheckTimer = setTimeout(() => {
    bridgeRecheckTimer = null;
    checkBridge().catch(() => undefined);
  }, delay);
}

function resetBridgeRecheck() {
  clearBridgeRecheck();
  bridgeRecheckAttempts = 0;
}

function bridgeErrorShouldAutoRetry(error = {}) {
  const code = String(error?.code || "");
  const status = Number(error?.status);
  if (error?.payload?.repair_required === true) return false;
  if ([
    "extension_pairing_not_authorized",
    "extension_origin_not_trusted",
    "agent_install_auth_corrupt",
    "agent_session_required",
    "agent_session_invalid",
    "agent_session_expired",
  ].includes(code)) return false;
  if (["welcome_check_timeout", "agent_session_timeout", "welcome_network_error"].includes(code)) return true;
  if (Number.isInteger(status) && status >= 500 && status <= 599) return true;
  return explicitNetworkError(error);
}

function explicitNetworkError(error = {}) {
  const code = String(error?.code || "").toLowerCase();
  if ([
    "welcome_network_error",
    "err_network",
    "err_connection_refused",
    "err_connection_reset",
    "econnrefused",
    "econnreset",
    "enotfound",
    "etimedout",
  ].includes(code)) return true;
  if (String(error?.name || "") === "AbortError") return true;
  return /failed to fetch|network\s*error|network request failed|load failed|connection (?:refused|reset|closed)|err_(?:connection|network|internet)|econnrefused|econnreset|enotfound|etimedout/i
    .test(String(error?.message || ""));
}

function bridgeErrorText(error = {}) {
  const code = String(error?.code || "");
  if (["extension_pairing_not_authorized", "extension_origin_not_trusted"].includes(code)) {
    return "当前扩展 ID 未授权，请重新加载扩展；仍未恢复再运行 Repair Dian Agent";
  }
  if (code === "agent_install_auth_corrupt" || error?.payload?.repair_required === true) {
    return "本机认证配置已损坏，请运行 Repair Dian Agent";
  }
  if (["agent_session_timeout", "welcome_check_timeout"].includes(code)) {
    return "本机认证响应超时，正在自动重新检测";
  }
  if (["agent_session_required", "agent_session_invalid", "agent_session_expired"].includes(code)) {
    return "认证会话已失效，自动刷新失败；请重新加载扩展，仍未恢复再运行 Repair Dian Agent";
  }
  const evidence = [code, Number(error?.status || 0) || "", error?.message]
    .filter(Boolean)
    .map((value) => String(value))
    .join(" · ")
    .slice(0, 120);
  return `本机认证暂时不可用${evidence ? `：${evidence}` : "，请稍后重新检测"}`;
}

function detectedDesktopPlatform(value = "") {
  if (value === "macos") return "macos";
  if (value === "windows") return "windows";
  return /Mac/i.test(navigator.platform || navigator.userAgent || "") ? "macos" : "windows";
}

function renderRepairInstruction(platform = "") {
  const node = document.getElementById("repair-agent-instruction");
  if (!node) return;
  node.textContent = detectedDesktopPlatform(platform) === "macos"
    ? "双击安装包中的 repair_dian_agent.command，它会重新注册 LaunchAgent 并恢复服务；完成后回到这里重新检测。"
    : "从 Windows 开始菜单运行“Repair Dian Agent”，它会在后台检查并恢复服务；完成后回到这里重新检测。";
}

function renderAgentUpgradeInstruction(platform = "", targetVersion = "") {
  const node = document.getElementById("repair-agent-instruction");
  if (!node) return;
  const versionLabel = targetVersion ? ` v${targetVersion}` : "新版";
  node.textContent = detectedDesktopPlatform(platform) === "macos"
    ? `本地 Agent 版本较旧。请重新运行${versionLabel} macOS 安装包完成覆盖升级，再回到这里重新检测。`
    : `本地 Agent 版本较旧。请运行${versionLabel} Windows 安装包完成覆盖升级，再回到这里重新检测。`;
}

function setRepairGuideVisible(visible) {
  const guide = document.getElementById("repair-agent-guide");
  if (guide) guide.hidden = !visible;
}

async function enableSentinel() {
  await chrome.runtime.sendMessage({
    type: "update-settings",
    settings: {
      operatingMode: "sentinel",
      autoSync: false,
      autoFullScan: false,
      privacyMode: true,
    },
  });
}

async function checkBridgeOnce() {
  const button = document.getElementById("check-bridge");
  const bridgeStatus = document.getElementById("bridge-status");
  const storeStatus = document.getElementById("store-status");
  button.disabled = true;
  bridgeStatus.textContent = "检测中";
  bridgeStatus.className = "state";
  try {
    // Liveness is intentionally public on localhost. Probe it before session
    // exchange so a pairing/configuration problem is never misreported as
    // "Agent not started".
    const health = await fetchHealth();
    renderRepairInstruction(health.platform);
    setRepairGuideVisible(location.hash === "#repair-agent");
    bridgeStatus.textContent = `已连接 v${health.version || ""}`;
    bridgeStatus.className = "state ok";
    let activationStatus;
    try {
      activationStatus = await fetchActivationStatus();
    } catch (error) {
      const agentVersion = String(health.version || "").trim();
      const oldAgentContract = [401, 403, 404].includes(Number(error?.status))
        && compareReleaseVersions(RUNTIME_EXTENSION_VERSION, agentVersion) === 1;
      if (oldAgentContract) {
        setRepairGuideVisible(true);
        renderAgentUpgradeInstruction(health.platform, RUNTIME_EXTENSION_VERSION);
        bridgeStatus.textContent = `本地 Agent v${agentVersion || "未知"} 需要升级`;
        bridgeStatus.className = "state warn";
        storeStatus.textContent = `浏览器扩展已是 v${RUNTIME_EXTENSION_VERSION}；请运行同版本安装包更新本地 Agent`;
        storeStatus.title = "agent_upgrade_required";
        storeStatus.className = "state warn";
        clearBridgeRecheck();
        return;
      }
      bridgeStatus.textContent = `Agent v${health.version || ""} 已运行，扩展激活状态未确认`;
      bridgeStatus.className = "state warn";
      storeStatus.textContent = "暂不能确认磁盘扩展与浏览器运行版本，连接已安全冻结";
      storeStatus.title = String(error?.code || error?.message || "activation_status_unavailable").slice(0, 160);
      storeStatus.className = "state warn";
      if (bridgeErrorShouldAutoRetry(error)) scheduleBridgeRecheck();
      else clearBridgeRecheck();
      return;
    }
    renderActivationLayers(health, activationStatus);
    if (activationNeedsPageReload(activationStatus)) {
      renderRuntimeVersionMismatch(activationStatus, bridgeStatus, storeStatus);
      requestBackgroundVersionReconcile();
      requestGuardedWelcomeReload(storeStatus, {
        // Give the background worker enough time to persist its reload guard
        // and call chrome.runtime.reload() before this document asks Chrome
        // for the replacement page. This avoids reloading the stale bundle a
        // second time during the same event-loop turn.
        delayMs: WELCOME_VERSION_SWITCH_DELAY_MS,
        guardedText: "浏览器仍运行旧扩展，已阻止重复自动刷新；请稍后按 Ctrl+R 或在扩展管理页重新加载",
        guardedTitle: "扩展版本自动重载已有一次，10 秒内不会重复",
        loadingText: "检测到磁盘扩展已更新，正在重新载入当前页面",
        loadingTitle: "磁盘扩展与浏览器运行版本不一致",
      });
      return;
    }
    const requiredVersion = String(activationStatus.required_extension_version || "").trim();
    const installedVersion = String(activationStatus.installed_extension_version || "").trim();
    const activationReady = activationStatus.activation?.ready === true
      && activationStatus.activation?.state === "active"
      && Boolean(requiredVersion)
      && installedVersion === requiredVersion
      && RUNTIME_EXTENSION_VERSION === requiredVersion;
    if (!activationReady) {
      const runtimeDirection = compareReleaseVersions(requiredVersion, RUNTIME_EXTENSION_VERSION);
      const storeUpdateRequired = activationStatus.activation?.state === "reload_required"
        && RUNTIME_EXTENSION_STORE_MANAGED
        && runtimeDirection === 1;
      const agentUpdateRequired = activationStatus.activation?.state === "reload_required"
        && runtimeDirection === -1;
      setRepairGuideVisible(!storeUpdateRequired && !agentUpdateRequired);
      bridgeStatus.textContent = agentUpdateRequired
        ? `本地 Agent v${health.version || ""} 低于浏览器扩展版本`
        : `Agent v${health.version || ""} 已运行，安装版本未就绪`;
      bridgeStatus.className = "state warn";
      if (storeUpdateRequired) {
        storeStatus.textContent = `浏览器商店扩展 v${RUNTIME_EXTENSION_VERSION || "未知"} 尚未更新到 v${requiredVersion || "未知"}；请在扩展管理页检查商店更新`;
      } else if (agentUpdateRequired) {
        storeStatus.textContent = `浏览器扩展为 v${RUNTIME_EXTENSION_VERSION}，本地 Agent 仍为 v${requiredVersion || "未知"}；请运行新版安装包更新 Agent`;
      } else {
        storeStatus.textContent = `Agent 需要 v${requiredVersion || "未知"}，磁盘扩展为 v${installedVersion || "未确认"}；请运行 Repair Dian Agent`;
      }
      storeStatus.title = `activation_not_ready · ${activationStatus.activation?.state || "invalid_contract"}`;
      storeStatus.className = "state warn";
      clearBridgeRecheck();
      return;
    }
    try {
      // DianBridgeAuth already clears and retries exactly once for the three
      // refreshable 401 session codes. Do not multiply that retry here.
      const [catalog, doudianTabOpen] = await Promise.all([fetchStores(), hasOpenDoudianTab()]);
      const storeState = welcomeStoreState(catalog, doudianTabOpen);
      storeStatus.textContent = storeState.text;
      storeStatus.title = storeState.title;
      storeStatus.className = storeState.className;
      clearWelcomeReloadGuard();
      resetBridgeRecheck();
    } catch (error) {
      if (recoverInvalidatedExtensionContext(error, storeStatus)) return;
      storeStatus.textContent = bridgeErrorText(error);
      storeStatus.title = String(error?.code || error?.message || "本机认证失败").slice(0, 160);
      storeStatus.className = "state warn";
      if (bridgeErrorShouldAutoRetry(error)) scheduleBridgeRecheck();
      else clearBridgeRecheck();
    }
  } catch (error) {
    setRepairGuideVisible(true);
    bridgeStatus.textContent = "未启动，请先运行安装包中的本地 Agent";
    bridgeStatus.className = "state warn";
    storeStatus.textContent = "本地 Agent 启动后检测";
    storeStatus.className = "state";
    if (bridgeErrorShouldAutoRetry(error)) scheduleBridgeRecheck();
  } finally {
    button.disabled = false;
  }
}

function checkBridge() {
  if (bridgeCheckInFlight) return bridgeCheckInFlight;
  bridgeCheckInFlight = checkBridgeOnce().finally(() => {
    bridgeCheckInFlight = null;
  });
  return bridgeCheckInFlight;
}

function requestBridgeRecheck() {
  resetBridgeRecheck();
  return checkBridge();
}

function openUrl(url) {
  chrome.tabs.create({ url });
}

async function openWorkbench(route = "") {
  try {
    const response = await chrome.runtime.sendMessage({ type: "open-workbench", route });
    if (response?.ok) return;
  } catch {
    // Fall through if the service worker restarted while the user clicked.
  }
  openUrl(`${WORKBENCH_URL}${route ? `#${route}` : ""}`);
}

document.getElementById("check-bridge").addEventListener("click", requestBridgeRecheck);
document.getElementById("open-workbench").addEventListener("click", () => openWorkbench());
document.getElementById("open-readiness").addEventListener("click", () => openWorkbench());
document.getElementById("finish-setup")?.addEventListener("click", () => openWorkbench());
document.getElementById("try-auto-delivery").addEventListener("click", () => openWorkbench("chengfang-demo"));
document.getElementById("repair-agent-recheck").addEventListener("click", requestBridgeRecheck);
document.getElementById("open-doudian").addEventListener(
  "click",
  () => openUrl("https://fxg.jinritemai.com/ffa/mshop/homepage/index"),
);
document.getElementById("open-qianchuan").addEventListener(
  "click",
  () => openUrl("https://qianchuan.jinritemai.com/"),
);

enableSentinel().catch(() => undefined);
renderRepairInstruction();
setRepairGuideVisible(location.hash === "#repair-agent");
setTimeout(checkBridge, 350);
window.addEventListener("focus", () => requestBridgeRecheck().catch(() => undefined));
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") requestBridgeRecheck().catch(() => undefined);
});
