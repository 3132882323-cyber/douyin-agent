const BRIDGE_URL = "http://127.0.0.1:8765";

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
  const response = await fetch(`${BRIDGE_URL}${path}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

async function bridgePost(path, body = {}) {
  const response = await fetch(`${BRIDGE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
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
    : "已识别，尚未读取";
}

function renderApi(status = {}, sync = {}) {
  const state = document.getElementById("api-state");
  const detail = document.getElementById("api-detail");
  const note = document.getElementById("api-sync-note");
  const button = document.getElementById("api-sync-button");
  button.disabled = !status.connected;
  if (!status.connected) {
    state.textContent = status.secret_saved ? "待授权" : "未连接";
    state.className = "tag warn";
    detail.textContent = status.secret_saved
      ? "打开完整工作台完成账号授权"
      : "尚未配置官方 API";
    note.textContent = "未连接时仍可手动同步当前网页。";
    return;
  }
  state.textContent = "已连接";
  state.className = "tag ok";
  detail.textContent = `已授权 ${status.account_count || 0} 个店铺账号`;
  if (sync.synced_at) {
    note.textContent = `上次 ${relativeTime(Number(sync.synced_at) * 1000)} · 保存 ${sync.saved_pages || 0} 类数据${sync.failure_count ? ` · ${sync.failure_count} 项需检查` : ""}`;
  } else {
    note.textContent = "点击后读取计划、7 日报表和素材，不修改投放。";
  }
}

async function render() {
  const response = await chrome.runtime.sendMessage({ type: "get-dashboard" });
  if (!response?.ok) throw new Error(response?.error || "无法读取扩展状态");
  const dashboard = response.dashboard;
  renderSource("doudian", dashboard);
  renderSource("qianchuan", dashboard);

  const overall = document.getElementById("overall");
  const title = document.getElementById("overall-title");
  const detail = document.getElementById("overall-detail");
  if (!dashboard.bridge?.ok) {
    overall.className = "overall error";
    title.textContent = "本地 Agent 未启动";
    detail.textContent = "打开完整工作台查看启动指引";
  } else {
    overall.className = "overall ok";
    title.textContent = "轻量哨兵运行正常";
    detail.textContent = "后台零自动扫描，只记录已打开页面状态";
  }
  document.getElementById("last-sync").textContent = relativeTime(dashboard.lastSyncAttempt);

  if (dashboard.bridge?.ok) {
    const [status, sync] = await Promise.all([
      bridgeGet("/oauth/oceanengine/status"),
      bridgeGet("/oauth/oceanengine/sync-status"),
    ]);
    renderApi(status, sync);
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
});

document.getElementById("sync-button").addEventListener("click", async (event) => {
  const button = event.currentTarget;
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
      button.textContent = "同步当前页面";
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
      button.disabled = false;
      button.textContent = "同步官方数据";
    }, 1400);
  }
});

document.getElementById("panel-button").addEventListener("click", async () => {
  await chrome.tabs.create({ url: chrome.runtime.getURL("sidepanel.html"), active: true });
  globalThis.close();
});

document.querySelectorAll("[data-open]").forEach((button) => {
  button.addEventListener("click", () => chrome.runtime.sendMessage({
    type: "open-platform",
    source: button.dataset.open,
  }));
});
