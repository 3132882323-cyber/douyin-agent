const BRIDGE_URL = "http://127.0.0.1:8765";
const PAGE_ASSISTANT_DISMISSED_KEY = "platformAssistantDismissedV2";
const PAGE_ASSISTANT_LEGACY_DISMISSED_KEY = "platformAssistantDismissedV1";
const bridgeAuthClient = globalThis.DianBridgeAuth.createClient({ baseUrl: BRIDGE_URL });

const PAGE_LABELS = {
  overview: "经营概览",
  orders: "订单",
  refunds: "售后",
  products: "商品",
  inventory: "库存",
  reviews: "评价",
  live: "直播",
  campaigns: "投放计划",
  plans: "官方计划",
  report: "投放报表",
  material_report: "素材报表",
  video_library: "视频库",
  shelf: "货架运营",
  short_video: "短视频",
};

function relativeTime(timestamp) {
  if (!timestamp) return "尚未同步";
  const seconds = Math.max(0, Math.round((Date.now() - Number(timestamp)) / 1000));
  if (seconds < 60) return "刚刚同步";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

async function bridgeGet(path) {
  return bridgeAuthClient.fetchJson(path);
}

async function bridgePost(path, body = {}) {
  return bridgeAuthClient.fetchJson(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
    body: JSON.stringify(body),
  });
}

async function openWorkbench(route = "") {
  try {
    const response = await chrome.runtime.sendMessage({ type: "open-workbench", route });
    if (response?.ok) return;
  } catch {
    // Fall back to a direct extension tab if the service worker restarted mid-click.
  }
  await chrome.tabs.create({
    url: chrome.runtime.getURL(`sidepanel.html${route ? `#${route}` : ""}`),
    active: true,
  });
}

function renderSource(source, dashboard) {
  const state = document.getElementById(`${source}-state`);
  const detail = document.getElementById(`${source}-detail`);
  const tabs = dashboard.tabs?.[source] || 0;
  const catalog = dashboard.catalog?.[source] || {};
  const pages = Object.entries(catalog).sort(
    (a, b) => (b[1].captured_at || 0) - (a[1].captured_at || 0),
  );
  if (!tabs) {
    state.textContent = "未打开";
    state.className = "tag warn";
    detail.textContent = pages.length
      ? `已有 ${pages.length} 类本地数据`
      : "需要时再打开后台";
    return;
  }
  state.textContent = `${tabs} 个页面`;
  state.className = "tag ok";
  detail.textContent = pages.length
    ? `${pages.map(([key]) => PAGE_LABELS[key] || key).slice(0, 2).join("、")} · ${relativeTime(pages[0][1].captured_at)}`
    : "已打开，尚未同步";
}

function renderApi(status = {}, sync = {}, syncError = "") {
  const state = document.getElementById("api-state");
  const detail = document.getElementById("api-detail");
  const note = document.getElementById("api-sync-note");
  const button = document.getElementById("api-sync-button");
  const refreshAvailable = status.refresh_available === true;
  button.disabled = !(status.connected || refreshAvailable);
  if (!status.connected && !refreshAvailable) {
    state.textContent = status.secret_saved ? "待授权" : "未连接";
    state.className = "tag warn";
    detail.textContent = status.secret_saved
      ? "打开完整工作台完成账号授权"
      : "尚未配置官方 API";
    note.textContent = "未连接时仍可手动同步当前网页。";
    return;
  }
  state.textContent = status.connected ? "已连接" : "待自动续期";
  state.className = status.connected ? "tag ok" : "tag warn";
  detail.textContent = status.connected
    ? `已授权 ${status.account_count || 0} 个店铺账号`
    : "刷新授权仍有效，点击同步会先自动续期";
  if (syncError) {
    note.textContent = "官方同步记录暂时无法读取；不影响本地 Agent、网页同步或安全演示。";
  } else if (sync.ok === false || sync.recovery_required === true || sync.corruption_detected === true) {
    state.textContent = "同步记录异常";
    state.className = "tag error";
    note.textContent = `官方同步记录异常：${sync.error?.message || "请重新执行一次只读同步恢复"}`;
  } else if (sync.synced_at) {
    note.textContent = `上次 ${relativeTime(Number(sync.synced_at) * 1000)} · 保存 ${sync.saved_pages || 0} 类数据${sync.failure_count ? ` · ${sync.failure_count} 项需检查` : ""}`;
  } else {
    note.textContent = "点击后读取计划、7 日报表和素材，不修改投放。";
  }
}

function renderApiUnavailable(error) {
  const state = document.getElementById("api-state");
  const detail = document.getElementById("api-detail");
  const note = document.getElementById("api-sync-note");
  const button = document.getElementById("api-sync-button");
  button.disabled = true;
  state.textContent = "暂不可用";
  state.className = "tag error";
  detail.textContent = "授权状态读取失败";
  note.textContent = `${error?.message || "官方 API 状态接口暂时不可用"}；本地 Agent、网页同步和安全演示仍可使用。`;
}

function setAgentRepairVisibility(visible) {
  const button = document.getElementById("repair-agent-button");
  if (button) button.hidden = !visible;
}

function repairFallbackMessage() {
  return /Mac/i.test(navigator.platform || navigator.userAgent || "")
    ? "唤醒失败，请双击安装包中的 repair_dian_agent.command"
    : "唤醒失败，请从开始菜单运行“Repair Dian Agent”";
}

async function renderApiAuxiliaryStatus() {
  const [statusResult, syncResult] = await Promise.allSettled([
    bridgeGet("/oauth/oceanengine/status"),
    bridgeGet("/oauth/oceanengine/sync-status"),
  ]);
  if (statusResult.status === "rejected") {
    renderApiUnavailable(statusResult.reason);
    return;
  }
  renderApi(
    statusResult.value,
    syncResult.status === "fulfilled" ? syncResult.value : {},
    syncResult.status === "rejected" ? syncResult.reason?.message || "读取失败" : "",
  );
}

async function renderPageAssistantPreference(message = "") {
  const doudianKey = `${PAGE_ASSISTANT_DISMISSED_KEY}.doudian`;
  const qianchuanKey = `${PAGE_ASSISTANT_DISMISSED_KEY}.qianchuan`;
  const stored = await chrome.storage.local.get([doudianKey, qianchuanKey, PAGE_ASSISTANT_LEGACY_DISMISSED_KEY]);
  const hiddenSources = [
    stored[doudianKey] === true ? "抖店" : "",
    stored[qianchuanKey] === true ? "千川" : "",
  ].filter(Boolean);
  const dismissed = stored[PAGE_ASSISTANT_LEGACY_DISMISSED_KEY] === true || hiddenSources.length > 0;
  const button = document.getElementById("show-page-assistant-button");
  const visible = document.getElementById("page-assistant-visible");
  const detail = document.getElementById("page-assistant-detail");
  button.hidden = !dismissed;
  visible.hidden = dismissed;
  if (dismissed) document.querySelector(".popup-more").open = true;
  detail.textContent = message || (dismissed
    ? `${hiddenSources.length ? `${hiddenSources.join("、")}页内助手已关闭` : "页内助手已关闭"}；可一键恢复`
    : "默认开启；打开抖店或千川页面时显示，可随时收起或关闭");
}

async function render() {
  const response = await chrome.runtime.sendMessage({ type: "get-dashboard" });
  if (!response?.ok) throw new Error(response?.error || "无法读取扩展状态");
  const dashboard = response.dashboard;
  const [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const activeUrl = String(activeTab?.url || "");
  const activeSource = activeUrl.startsWith("https://fxg.jinritemai.com/")
    ? "doudian"
    : activeUrl.startsWith("https://qianchuan.jinritemai.com/") || activeUrl.startsWith("https://buyin.jinritemai.com/")
      ? "qianchuan"
      : "";
  renderSource("doudian", dashboard);
  renderSource("qianchuan", dashboard);

  const overall = document.getElementById("overall");
  const title = document.getElementById("overall-title");
  const detail = document.getElementById("overall-detail");
  const nextTitle = document.getElementById("popup-next-title");
  const nextDetail = document.getElementById("popup-next-detail");
  const nextButton = document.getElementById("sync-button");
  if (dashboard.bridge?.liveness_ok && dashboard.bridge?.authenticated !== true) {
    overall.className = "overall warn";
    title.textContent = "Agent 已运行，扩展未配对";
    detail.textContent = "请在扩展管理页点“重新加载”；仍未恢复时再运行 Repair Dian Agent。";
    setAgentRepairVisibility(true);
    nextTitle.textContent = "先完成扩展身份验收";
    nextDetail.textContent = `当前错误：${dashboard.bridge?.error_code || "agent_session_required"}`;
    nextButton.dataset.action = "open_extension_manager";
    nextButton.dataset.idleLabel = "打开扩展管理页";
    nextButton.textContent = nextButton.dataset.idleLabel;
  } else if (dashboard.bridge?.liveness_ok && dashboard.bridge?.version_match === false) {
    overall.className = "overall warn";
    title.textContent = "Agent 与扩展版本不一致";
    detail.textContent = "Agent 已运行，但浏览器仍在使用旧扩展；请到扩展管理页点“重新加载”。";
    setAgentRepairVisibility(true);
    nextTitle.textContent = "重新加载后再继续同步";
    nextDetail.textContent = dashboard.bridge?.error || "版本尚未完成验收";
    nextButton.dataset.action = "open_extension_manager";
    nextButton.dataset.idleLabel = "打开扩展管理页";
    nextButton.textContent = nextButton.dataset.idleLabel;
  } else if (!dashboard.bridge?.ok) {
    overall.className = "overall error";
    title.textContent = "本地 Agent 未启动";
    detail.textContent = "点一下即可查看后台修复方法";
    setAgentRepairVisibility(true);
    nextTitle.textContent = "先恢复本地连接";
    nextDetail.textContent = "不会弹出命令行窗口，修复完成后再继续同步。";
    nextButton.dataset.action = "repair_agent";
    nextButton.dataset.idleLabel = "修复本地连接";
    nextButton.textContent = nextButton.dataset.idleLabel;
  } else {
    overall.className = "overall ok";
    title.textContent = "已经准备好";
    detail.textContent = "数据只在你点击时读取，并保存在本机";
    setAgentRepairVisibility(false);
    if (activeSource) {
      nextTitle.textContent = activeSource === "doudian" ? "同步这个抖店页面" : "同步这个千川页面";
      nextDetail.textContent = "只读取你现在看到的页面，完成后可在工作台查看结果。";
      nextButton.dataset.action = "sync_current";
      nextButton.dataset.idleLabel = "同步此页";
    } else {
      nextTitle.textContent = "打开抖店，直接开始";
      nextDetail.textContent = "保持登录即可，不需要在插件里填写账号密码。";
      nextButton.dataset.action = "open_doudian";
      nextButton.dataset.idleLabel = "打开抖店";
    }
    nextButton.textContent = nextButton.dataset.idleLabel;
  }
  const lastSuccessfulSync = Number(dashboard.lastSuccessfulSync || 0);
  const lastSyncAttempt = Number(dashboard.lastSyncAttempt || 0);
  document.getElementById("last-sync").textContent = lastSuccessfulSync
    ? `上次成功 ${relativeTime(lastSuccessfulSync)}`
    : lastSyncAttempt
      ? `最近尝试 ${relativeTime(lastSyncAttempt)} · 尚未成功`
      : "尚未成功同步";

  if (dashboard.bridge?.ok) {
    await renderApiAuxiliaryStatus();
  } else {
    renderApi({}, {});
  }
}

document.addEventListener("DOMContentLoaded", () => {
  render().catch((error) => {
    document.getElementById("overall-title").textContent = "状态读取失败";
    document.getElementById("overall-detail").textContent = error.message;
    document.getElementById("overall").className = "overall error";
  });
  renderPageAssistantPreference().catch(() => undefined);
});

document.getElementById("sync-button").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const action = button.dataset.action || "sync_current";
  if (action === "repair_agent") {
    document.getElementById("repair-agent-button").click();
    return;
  }
  if (action === "open_extension_manager") {
    const response = await chrome.runtime.sendMessage({ type: "open-extension-manager" });
    if (!response?.ok) throw new Error(response?.error || "无法打开扩展管理页");
    globalThis.close();
    return;
  }
  if (action === "open_doudian") {
    await chrome.runtime.sendMessage({ type: "open-platform", source: "doudian" });
    globalThis.close();
    return;
  }
  button.disabled = true;
  button.textContent = "正在同步…";
  try {
    const response = await chrome.runtime.sendMessage({ type: "sync-current-page" });
    if (!response?.ok) throw new Error(response?.error || "同步失败");
    button.textContent = "同步完成";
    await render();
  } catch (error) {
    button.textContent = "同步失败";
    document.getElementById("overall-detail").textContent = error.message || "请先打开抖店或千川页面";
  } finally {
    setTimeout(() => {
      button.disabled = false;
      button.textContent = button.dataset.idleLabel || "同步当前页面";
    }, 1200);
  }
});

document.getElementById("api-sync-button").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const note = document.getElementById("api-sync-note");
  button.disabled = true;
  button.textContent = "正在读取…";
  note.textContent = "正在读取授权账号、计划、7 日报表和视频素材。";
  try {
    const result = await bridgePost("/oauth/oceanengine/sync", { days: 7 });
    button.textContent = "同步完成";
    note.textContent = `已同步 ${result.account_count || 0} 个店铺，保存 ${result.saved_pages || 0} 类数据。`;
  } catch (error) {
    button.textContent = "同步失败";
    note.textContent = error.message || "官方 API 暂时不可用";
  } finally {
    setTimeout(() => {
      button.textContent = "同步官方数据";
      renderApiAuxiliaryStatus().catch(() => { button.disabled = true; });
    }, 1400);
  }
});

document.getElementById("panel-button").addEventListener("click", async () => {
  await openWorkbench();
  globalThis.close();
});

document.getElementById("show-page-assistant-button").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在恢复…";
  try {
    const response = await chrome.runtime.sendMessage({ type: "show-platform-assistant" });
    if (!response?.ok) throw new Error(response?.error || "页内助手恢复失败");
    await renderPageAssistantPreference(response.assistants > 0
      ? "已恢复到当前打开的抖店或千川页面"
      : "已恢复；打开抖店或千川页面后会自动显示");
    document.getElementById("page-assistant-setting").focus();
  } catch (error) {
    document.getElementById("page-assistant-detail").textContent = error?.message || "恢复失败，请重试";
  } finally {
    button.disabled = false;
    button.textContent = "重新显示";
  }
});

document.getElementById("demo-button").addEventListener("click", async () => {
  await openWorkbench("chengfang-demo");
  globalThis.close();
});

document.getElementById("repair-agent-button").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const detail = document.getElementById("overall-detail");
  button.disabled = true;
  button.textContent = "查看修复方法…";
  detail.textContent = "正在打开不弹命令行窗口的 Agent 修复指引。";
  try {
    const result = await chrome.runtime.sendMessage({ type: "repair-agent" });
    if (!result?.ok) throw new Error(result?.error || "修复指引打开失败");
    globalThis.close();
  } catch (error) {
    detail.textContent = error?.message || repairFallbackMessage();
  } finally {
    button.disabled = false;
    button.textContent = "修复 Agent";
  }
});

document.querySelectorAll("[data-open]").forEach((button) => {
  button.addEventListener("click", () => chrome.runtime.sendMessage({
    type: "open-platform",
    source: button.dataset.open,
  }));
});
