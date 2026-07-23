const PAGE_LABELS = {
  overview: "经营概览",
  orders: "订单",
  refunds: "售后",
  products: "商品",
  inventory: "库存",
  reviews: "评价",
  live: "直播",
  compass: "罗盘",
  funds: "资金",
  campaigns: "投放计划",
  report: "投放报表",
  materials: "素材",
  account: "账户",
  affiliate: "精选联盟",
  shelf: "货架运营",
  short_video: "短视频",
  image_text: "图文",
  search: "搜索运营",
  recommend_card: "推荐卡",
  qianchuan_campaigns: "千川商品投放",
  qianchuan_live: "千川直播投放",
  qianchuan_report: "千川数据",
  unknown: "其他页面",
};

function relativeTime(timestamp) {
  if (!timestamp) return "尚未同步";
  const seconds = Math.max(0, Math.round((Date.now() - Number(timestamp)) / 1000));
  if (seconds < 60) return "刚刚同步";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  return `${Math.floor(seconds / 3600)} 小时前`;
}

function renderSource(source, dashboard) {
  const state = document.getElementById(`${source}-state`);
  const detail = document.getElementById(`${source}-detail`);
  const tabs = dashboard.tabs?.[source] || 0;
  const catalog = dashboard.catalog?.[source] || {};
  const pages = Object.entries(catalog).sort((a, b) => (b[1].captured_at || 0) - (a[1].captured_at || 0));
  if (!tabs) {
    state.textContent = "未打开";
    state.className = "tag warn";
    detail.textContent = pages.length ? `缓存：${pages.map(([key]) => PAGE_LABELS[key] || key).slice(0, 2).join("、")}` : "请先打开并登录后台";
    return;
  }
  state.textContent = `${tabs} 个页面`;
  state.className = "tag ok";
  detail.textContent = pages.length
    ? `${pages.map(([key]) => PAGE_LABELS[key] || key).slice(0, 3).join("、")} · ${relativeTime(pages[0][1].captured_at)}`
    : "页面已连接，等待首次同步";
}

async function render() {
  const response = await chrome.runtime.sendMessage({ type: "get-dashboard" });
  if (!response?.ok) throw new Error(response?.error || "无法读取扩展状态");
  const dashboard = response.dashboard;
  renderSource("doudian", dashboard);
  renderSource("qianchuan", dashboard);
  document.getElementById("privacy-toggle").checked = dashboard.settings?.privacyMode !== false;
  const scan = dashboard.fullScan || {};
  const scanButton = document.getElementById("full-scan-button");
  scanButton.disabled = scan.status === "running";
  scanButton.textContent = scan.status === "running" ? `正在巡检 ${scan.index || 0}/${scan.total || 18}` : "自动获取全店数据";
  document.getElementById("scan-detail").textContent = scan.status === "running"
    ? `正在采集：${scan.current || "准备中"}`
    : scan.finished_at ? `上次成功 ${scan.success || 0} 页，失败 ${scan.failed || 0} 页` : "自动巡检核心经营页面，完成后生成诊断";

  const overall = document.getElementById("overall");
  const title = document.getElementById("overall-title");
  const detail = document.getElementById("overall-detail");
  const totalTabs = (dashboard.tabs?.doudian || 0) + (dashboard.tabs?.qianchuan || 0);
  if (!dashboard.bridge?.ok) {
    overall.className = "overall error";
    title.textContent = "本地 Agent 未启动";
    detail.textContent = "首次使用请双击 bridge/enable_autostart.bat";
  } else if (!totalTabs) {
    overall.className = "overall warn";
    title.textContent = "等待后台页面";
    detail.textContent = "打开抖店或千川后即可读取";
  } else {
    overall.className = "overall ok";
    title.textContent = "经营数据链路正常";
    detail.textContent = `已连接 ${totalTabs} 个后台页面，数据仅保存在本机`;
  }
  document.getElementById("last-sync").textContent = relativeTime(dashboard.lastSyncAttempt);
}

document.addEventListener("DOMContentLoaded", () => render().catch((error) => {
  document.getElementById("overall-title").textContent = "状态读取失败";
  document.getElementById("overall-detail").textContent = error.message;
  document.getElementById("overall").className = "overall error";
}));

document.getElementById("sync-button").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在同步…";
  try {
    await chrome.runtime.sendMessage({ type: "manual-sync" });
    button.textContent = "同步完成";
    await render();
  } catch {
    button.textContent = "同步失败";
  } finally {
    setTimeout(() => { button.disabled = false; button.textContent = "同步当前页面"; }, 1200);
  }
});

document.getElementById("full-scan-button").addEventListener("click", async (event) => {
  event.currentTarget.disabled = true;
  event.currentTarget.textContent = "正在启动…";
  await chrome.runtime.sendMessage({ type: "start-full-scan" });
  await render();
});

document.getElementById("panel-button").addEventListener("click", async () => {
  const currentWindow = await chrome.windows.getCurrent();
  await chrome.sidePanel.open({ windowId: currentWindow.id });
  globalThis.close();
});

document.getElementById("privacy-toggle").addEventListener("change", async (event) => {
  await chrome.runtime.sendMessage({
    type: "update-settings",
    settings: { privacyMode: event.currentTarget.checked },
  });
});

document.querySelectorAll("[data-open]").forEach((button) => {
  button.addEventListener("click", () => chrome.runtime.sendMessage({ type: "open-platform", source: button.dataset.open }));
});
