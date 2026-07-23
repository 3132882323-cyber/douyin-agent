/** 店策 Agent - MV3 service worker */

const BRIDGE_URL = "http://127.0.0.1:8765";
const QIANCHUAN_ENTRY_URL = "https://qianchuan.jinritemai.com/";
const ALARM_NAME = "dian-agent-sync";
const FULL_SCAN_ALARM = "dian-agent-full-scan";
const DEFAULT_SETTINGS = {
  autoSync: false,
  intervalMinutes: 5,
  autoFullScan: false,
  fullScanIntervalHours: 6,
  privacyMode: true,
};

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
  { id: "qianchuan_overview", expectedPageType: "overview", label: "千川经营首页", source: "qianchuan", url: QIANCHUAN_ENTRY_URL, fallbackUrls: ["https://qianchuan.jinritemai.com/home"], waitMs: 4500 },
  { id: "qianchuan_campaigns", expectedPageType: "campaigns", label: "千川商品推广", source: "qianchuan", url: "https://qianchuan.jinritemai.com/uni-prom", tabTexts: ["商品全域推广", "商品推广"], waitMs: 5500, harvestList: true, collectTimeoutMs: 24000 },
  { id: "qianchuan_live", label: "千川直播推广", source: "qianchuan", url: "https://qianchuan.jinritemai.com/uni-prom", tabTexts: ["直播全域推广", "直播推广", "直播间推广"], waitMs: 5500, harvestList: true, collectTimeoutMs: 24000 },
  { id: "qianchuan_live_dashboard", expectedPageType: "live_dashboard", label: "千川直播大屏", source: "qianchuan", url: "https://qianchuan.jinritemai.com/board-next", waitMs: 5500 },
  { id: "qianchuan_video_library", expectedPageType: "video_library", label: "千川视频库", source: "qianchuan", url: "https://qianchuan.jinritemai.com/tools/creative-management/video-library", waitMs: 5000, harvestList: true, collectTimeoutMs: 24000 },
];

const SOURCE_PATTERNS = {
  doudian: ["https://fxg.jinritemai.com/*"],
  qianchuan: ["https://qianchuan.jinritemai.com/*", "https://buyin.jinritemai.com/*"],
};

const SOURCE_URLS = {
  doudian: "https://fxg.jinritemai.com/ffa/mshop/homepage/index",
  qianchuan: QIANCHUAN_ENTRY_URL,
};

// Serialize read-modify-write operations so concurrent tabs cannot overwrite
// each other's catalog or status entries.
let storageMutationQueue = Promise.resolve();
let fullScanPromise = null;
let fullScanCancelled = false;

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

chrome.runtime.onInstalled.addListener(async () => {
  const stored = await chrome.storage.local.get("settings");
  await chrome.storage.local.set({ settings: { ...DEFAULT_SETTINGS, ...(stored.settings || {}), autoSync: false, autoFullScan: false } });
  await configureAlarm();
  await updateStatus("system", "扩展已就绪");
});

chrome.runtime.onStartup.addListener(configureAlarm);

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) syncAll("scheduled");
  if (alarm.name === FULL_SCAN_ALARM) startFullScan("scheduled").catch(() => undefined);
});

async function getSettings() {
  const stored = await chrome.storage.local.get("settings");
  return { ...DEFAULT_SETTINGS, ...(stored.settings || {}) };
}

async function configureAlarm() {
  const settings = await getSettings();
  await chrome.alarms.clear(ALARM_NAME);
  await chrome.alarms.clear(FULL_SCAN_ALARM);
  if (settings.autoSync) {
    chrome.alarms.create(ALARM_NAME, {
      delayInMinutes: 1,
      periodInMinutes: Math.max(1, Number(settings.intervalMinutes) || 5),
    });
  }
  if (settings.autoFullScan) {
    chrome.alarms.create(FULL_SCAN_ALARM, {
      delayInMinutes: 10,
      periodInMinutes: Math.max(60, Number(settings.fullScanIntervalHours) * 60 || 360),
    });
  }
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function withTimeout(promise, timeoutMs, message) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

async function setFullScanState(patch) {
  const updates = await mutateLocalStorage(["fullScan"], ({ fullScan }) => ({ fullScan: { ...(fullScan || {}), ...patch } }));
  if (updates.fullScan) {
    await fetch(`${BRIDGE_URL}/scan-status`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
      body: JSON.stringify(updates.fullScan),
    }).catch(() => undefined);
  }
}

function waitForTabReady(tabId, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      error ? reject(error) : resolve();
    };
    const listener = (updatedId, changeInfo) => {
      if (updatedId === tabId && changeInfo.status === "complete") finish();
    };
  const timer = setTimeout(() => finish(new Error("页面加载超时")), timeoutMs);
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function navigateScanTab(tabId, url) {
  const current = await chrome.tabs.get(tabId);
  const ready = waitForTabReady(tabId, 25000);
  if ((current.url || "").split("#")[0] === url.split("#")[0]) await chrome.tabs.reload(tabId);
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
    throw new Error(`页面无法访问或被浏览器中止（ERR_FAILED）；请检查网络后重试。${error.message ? ` ${error.message}` : ""}`);
  }
  if (!page?.href) throw new Error("页面没有成功加载，请重试");
  const loadedUrl = new URL(page.href);
  if (source === "qianchuan" && (loadedUrl.pathname === "/login" || /巨量千川-登录|登录/.test(page.title || ""))) {
    throw new Error("千川登录已失效，请先在巨量千川完成登录，再点击巡查");
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

async function scanOnePage(tabId, page, reason, accountKey = "") {
  let lastError;
  const candidateUrls = [page.url, ...(page.fallbackUrls || [])];
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    try {
      const targetUrl = candidateUrls[Math.min(attempt - 1, candidateUrls.length - 1)];
      await navigateScanTab(tabId, targetUrl);
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
      const response = await withTimeout(
        collectFromTab(page.source, { id: tabId }, `${scanMode}-${reason}-${page.id}`),
        collectTimeoutMs,
        `${page.label}采集超过 ${Math.round(collectTimeoutMs / 1000)} 秒，已跳过`,
      );
      if (!response?.ok) throw new Error(response?.error || "采集失败");
      if (page.source === "qianchuan" && accountKey) {
        if (!response.account?.key) throw new Error("未识别当前千川账号，请先在千川后台确认账号后重试");
        if (response.account.key !== accountKey) throw new Error(`当前千川账号为“${response.account.label || "其他账号"}”，与所选分析账号不一致`);
      }
      if (response.page_type === "unknown") throw new Error("页面类型未识别");
      const expectedPageType = page.expectedPageType || page.id;
      if (response.page_type !== expectedPageType) throw new Error(`页面识别为 ${response.page_type}，预期为 ${expectedPageType}`);
      return { id: page.id, label: page.label, ok: true, page_type: response.page_type, quality: response.quality || null };
    } catch (error) {
      lastError = error;
      if (attempt < 2) await sleep(1200);
    }
  }
  return { id: page.id, label: page.label, ok: false, error: lastError?.message || String(lastError) };
}

async function runFullScan(reason = "manual", pageIds = null, accountKey = "") {
  fullScanCancelled = false;
  const startedAt = Date.now();
  const results = [];
  const targeted = Array.isArray(pageIds) && pageIds.length > 0;
  const previousScan = (await chrome.storage.local.get("fullScan")).fullScan || {};
  const scanPages = targeted ? FULL_SCAN_PAGES.filter((page) => pageIds.includes(page.id)) : FULL_SCAN_PAGES;
  let scanTab;
  await setFullScanState({ status: "running", reason, account_key: accountKey || "", started_at: startedAt, finished_at: null, current: "准备巡检", index: 0, total: scanPages.length, success: 0, failed: 0, low_quality: 0, results: [] });
  try {
    scanTab = await chrome.tabs.create({ url: "about:blank", active: false });
    for (let index = 0; index < scanPages.length; index += 1) {
      if (fullScanCancelled) break;
      const page = scanPages[index];
      await setFullScanState({ current: page.label, index: index + 1 });
      const result = await scanOnePage(scanTab.id, page, reason, accountKey);
      results.push(result);
      await setFullScanState({ results, success: results.filter((item) => item.ok).length, failed: results.filter((item) => !item.ok).length, low_quality: results.filter((item) => item.ok && Number(item.quality?.score || 0) < 50).length });
      // Give the browser UI and the platform page a short idle window between
      // pages so a long scan does not monopolize the renderer.
      await sleep(350);
    }
    let finalResults = results;
    if (targeted && !fullScanCancelled) {
      const merged = new Map((previousScan.results || []).map((item) => [item.id, item]));
      results.forEach((item) => merged.set(item.id, item));
      finalResults = FULL_SCAN_PAGES.map((page) => merged.get(page.id)).filter(Boolean);
    }
    const incomplete = finalResults.length < FULL_SCAN_PAGES.length;
    const status = fullScanCancelled ? "cancelled" : finalResults.some((item) => !item.ok) || incomplete ? "partial" : "completed";
    await setFullScanState({ status, current: "", finished_at: Date.now(), results: finalResults, index: targeted ? finalResults.length : scanPages.length, total: targeted ? FULL_SCAN_PAGES.length : scanPages.length, success: finalResults.filter((item) => item.ok).length, failed: finalResults.filter((item) => !item.ok).length, low_quality: finalResults.filter((item) => item.ok && Number(item.quality?.score || 0) < 50).length });
    await chrome.storage.local.set({ lastSyncAttempt: Date.now() });
    if (!fullScanCancelled) {
      await fetch(`${BRIDGE_URL}/reports/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
        body: "{}",
      }).catch(() => undefined);
    }
    return { status, results };
  } catch (error) {
    await setFullScanState({ status: "error", current: "", finished_at: Date.now(), error: error.message || String(error), results, success: results.filter((item) => item.ok).length, failed: results.filter((item) => !item.ok).length });
    return { status: "error", error: error.message || String(error), results };
  } finally {
    if (scanTab?.id) await chrome.tabs.remove(scanTab.id).catch(() => undefined);
  }
}

function startFullScan(reason = "manual", pageIds = null, accountKey = "") {
  if (!fullScanPromise) {
    fullScanPromise = runFullScan(reason, pageIds, accountKey).finally(() => { fullScanPromise = null; });
  }
  return fullScanPromise;
}

async function querySourceTabs(source) {
  return chrome.tabs.query({ url: SOURCE_PATTERNS[source] });
}

async function collectFromTab(source, tab, reason) {
  try {
    return await chrome.tabs.sendMessage(tab.id, { type: "collect-now", reason });
  } catch (firstError) {
    const platformScript = source === "doudian" ? "content-doudian.js" : "content-qianchuan.js";
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: ["content-common.js", platformScript],
    });
    await new Promise((resolve) => setTimeout(resolve, 250));
    try {
      return await chrome.tabs.sendMessage(tab.id, { type: "collect-now", reason: `${reason}-reinjected` });
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
      if (response?.ok) collected += 1;
      else errors.push(response?.error || `标签页 ${tab.id} 未返回数据`);
    } catch (error) {
      errors.push(error.message || String(error));
    }
  }

  if (collected) {
    await updateStatus(source, `已同步 ${collected} 个页面`, "ok");
  } else {
    await updateStatus(source, "页面存在，但采集脚本未就绪；请刷新页面", "error");
  }
  return { source, tabs: tabs.length, collected, errors };
}

async function syncAll(reason = "manual") {
  const results = await Promise.all([
    syncSource("doudian", reason),
    syncSource("qianchuan", reason),
  ]);
  await chrome.storage.local.set({ lastSyncAttempt: Date.now() });
  return results;
}

async function syncCurrentPage(sourceOnly = "") {
  const [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const url = activeTab?.url || "";
  const source = url.startsWith("https://fxg.jinritemai.com/") ? "doudian"
    : url.startsWith("https://qianchuan.jinritemai.com/") || url.startsWith("https://buyin.jinritemai.com/") ? "qianchuan" : "";
  if (!activeTab?.id || !source) throw new Error("当前页面不是抖店或巨量千川后台");
  if (sourceOnly && source !== sourceOnly) throw new Error("请先切换到需要读取的巨量千川页面");
  await inspectPlatformPage(activeTab.id, source);
  const response = await collectFromTab(source, activeTab, "manual-current-page");
  if (!response?.ok) throw new Error(response?.error || "当前页面读取失败");
  await chrome.storage.local.set({ lastSyncAttempt: Date.now() });
  return { source, page_type: response.page_type, quality: response.quality, account: response.account || null };
}

async function storeAndPush(source, snapshot) {
  if (!SOURCE_PATTERNS[source] || !snapshot || typeof snapshot !== "object") {
    throw new Error("无效的数据快照");
  }

  const pageType = String(snapshot.page_type || "unknown").replace(/[^a-z0-9_-]/gi, "_");
  const capturedAt = Number(snapshot.captured_at || snapshot.timestamp || Date.now());
  let bridgeResult;

  // Push first. A browser-cache quota error must never prevent the core sync.
  try {
    const response = await fetch(`${BRIDGE_URL}/push`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Dian-Agent": "2",
      },
      body: JSON.stringify({ source, data: snapshot }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    bridgeResult = { ok: true };
  } catch (error) {
    bridgeResult = { ok: false, error: error.message || String(error) };
  }

  // Keep only small metadata in chrome.storage.local. Full snapshots are
  // partitioned and retained by the local bridge.
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

  await updateStatus(
    "bridge",
    bridgeResult.ok ? "本地 Agent 已连接" : "本地 Agent 未启动",
    bridgeResult.ok ? "ok" : "error",
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

async function checkBridge() {
  try {
    const response = await fetch(`${BRIDGE_URL}/health`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    await updateStatus("bridge", "本地 Agent 已连接", "ok");
    return { ok: true, data };
  } catch (error) {
    await updateStatus("bridge", "本地 Agent 未启动", "error");
    return { ok: false, error: error.message || String(error) };
  }
}

async function getDashboard() {
  const [stored, doudianTabs, qianchuanTabs, bridge] = await Promise.all([
    chrome.storage.local.get(["status", "catalog", "settings", "lastSyncAttempt", "fullScan"]),
    querySourceTabs("doudian"),
    querySourceTabs("qianchuan"),
    checkBridge(),
  ]);
  let fullScan = stored.fullScan || { status: "idle", total: FULL_SCAN_PAGES.length, index: 0, success: 0, failed: 0 };
  // MV3 service workers can be restarted by Chrome. If no in-memory scan is
  // running, a persisted "running" state is stale and must not leave the UI
  // looking frozen forever.
  if (fullScan.status === "running" && !fullScanPromise) {
    fullScan = {
      ...fullScan,
      status: "interrupted",
      current: "",
      finished_at: Date.now(),
      error: "上次巡检因扩展更新或浏览器休眠而中断，请重新点击巡检",
    };
    await setFullScanState(fullScan);
  }
  return {
    status: stored.status || {},
    catalog: stored.catalog || {},
    settings: { ...DEFAULT_SETTINGS, ...(stored.settings || {}) },
    lastSyncAttempt: stored.lastSyncAttempt || null,
    fullScan,
    tabs: { doudian: doudianTabs.length, qianchuan: qianchuanTabs.length },
    bridge,
  };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    if (message.type === "page-data") {
      sendResponse(await storeAndPush(message.source, message.data));
      return;
    }
    if (message.type === "manual-sync" || message.type === "manual-poll") {
      sendResponse({ ok: true, results: await syncAll("manual") });
      return;
    }
    if (message.type === "sync-current-page") {
      sendResponse({ ok: true, result: await syncCurrentPage("") });
      return;
    }
    if (message.type === "sync-current-qianchuan") {
      sendResponse({ ok: true, result: await syncCurrentPage("qianchuan") });
      return;
    }
    if (message.type === "start-full-scan") {
      if (fullScanPromise) {
        sendResponse({ ok: true, started: false, message: "巡检正在进行" });
      } else {
        startFullScan("manual", Array.isArray(message.page_ids) ? message.page_ids : null, String(message.account_key || "")).catch((error) => setFullScanState({ status: "error", current: "", finished_at: Date.now(), error: error.message || String(error) }));
        sendResponse({ ok: true, started: true });
      }
      return;
    }
    if (message.type === "retry-failed-scan") {
      const stored = await chrome.storage.local.get("fullScan");
      const failedIds = (stored.fullScan?.results || []).filter((item) => !item.ok).map((item) => item.id);
      if (!failedIds.length) sendResponse({ ok: false, error: "没有需要重试的失败页面" });
      else {
        startFullScan("retry-failed", failedIds).catch(() => undefined);
        sendResponse({ ok: true, started: true, total: failedIds.length });
      }
      return;
    }
    if (message.type === "cancel-full-scan") {
      fullScanCancelled = true;
      sendResponse({ ok: true });
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
    sendResponse({ ok: false, error: "未知消息" });
  })().catch((error) => sendResponse({ ok: false, error: error.message || String(error) }));
  return true;
});
