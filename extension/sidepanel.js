const BRIDGE_URL = "http://127.0.0.1:8765";
const LABELS = {
  doudian: "抖店", qianchuan: "千川", overview: "概览", orders: "订单",
  refunds: "售后", products: "商品", inventory: "库存", reviews: "评价",
  live: "直播", compass: "罗盘", funds: "资金", campaigns: "计划",
  report: "报表", materials: "素材", video_library: "视频库", live_dashboard: "直播大屏", account: "账户", shelf: "货架",
  qianchuan_live: "直播推广", qianchuan_campaigns: "商品推广", qianchuan_live_dashboard: "直播大屏", qianchuan_video_library: "视频库", unknown: "其他",
};
const REPORT_TEMPLATE_LABELS = {
  default: "默认经营日报",
  brief: "老板简报",
  handover: "运营交接日志",
  custom: "自定义模板",
};

let latestBrief = "";
let currentRole = "运营总管";
let currentOps = null;
let currentOperationsContext = null;
let workbenchScene = "daily";
let templateChecks = {};
let focusOnlyActionable = true;
let managerQueueExpanded = false;
let currentPreflightSession = null;
let qianchuanSyncPromise = null;
let oceanengineStatusPoller = null;
let currentOperationContext = null;

const ROLE_WORKBENCH = {
  "运营总管": {
    title: "运营总管工作台",
    description: "跨岗位查看风险、待确认动作和今日验收结果",
    tasks: [
      ["assign_top_actions", "确认今日三件事", "每项任务明确负责人、截止时间和验收指标"],
      ["review_pending_actions", "审核待确认投放方案", "账号、计划、当前值和目标值均已核对"],
      ["close_daily_loop", "检查昨日任务结果", "已完成、待观察和无效建议都有结论"],
    ],
  },
  "货架运营": {
    title: "货架运营工作台",
    description: "聚焦曝光、点击、成交和商品承接问题",
    tasks: [
      ["shelf_funnel", "核对货架漏斗", "定位曝光、点击或成交环节的最大损失"],
      ["shelf_assets", "检查主图与标题", "高曝光低点击商品已建立优化任务"],
      ["shelf_search", "检查搜索和推荐卡", "潜力商品的搜索词与推荐卡状态已核对"],
    ],
  },
  "直播运营": {
    title: "直播运营工作台",
    description: "围绕进房、商品点击、成交和直播承接执行",
    tasks: [
      ["live_funnel", "核对直播漏斗", "进房、商品点击和成交瓶颈已定位"],
      ["live_script", "检查话术和商品顺序", "开场钩子、主推品和利益点已经确认"],
      ["live_review", "记录异常时间点", "流量或转化异常已关联到对应直播时段"],
    ],
  },
  "投放运营": {
    title: "千川投手工作台",
    description: "只处理止损、观察、换素材和具备条件的放量计划",
    tasks: [
      ["ad_account", "锁定千川账号与日期", "账号、统计周期和归因口径已核对"],
      ["ad_risk", "处理高消耗低转化计划", "每项调整都有依据、幅度和观察窗口"],
      ["ad_creative", "检查计划与素材表现", "衰退素材和待测素材已进入测试清单"],
    ],
  },
  "商品运营": {
    title: "商品运营工作台",
    description: "优先处理断货风险、可售天数和投放库存冲突",
    tasks: [
      ["stock_risk", "检查缺货与极低库存", "高风险 SKU 已补货或限制流量"],
      ["stock_cover", "核对预计可售天数", "直播主推品和放量品库存满足计划"],
      ["stock_sync", "同步投放与库存动作", "缺货商品没有继续扩大千川消耗"],
    ],
  },
};

const SCENE_WORKBENCH = {
  daily: {
    label: "日常经营",
    intro: "完成固定检查，再处理系统诊断出的异常任务。",
    task: ["scene_daily_data", "先确认数据体检单", "失败页已重试，低质量数据已经人工复核"],
  },
  pre_live: {
    label: "开播前",
    intro: "开播前先锁定商品、库存、素材和投放边界。",
    task: ["scene_pre_live", "完成开播前联检", "主推品、库存、素材、预算和直播目标一致"],
  },
  live: {
    label: "直播中",
    intro: "直播中只看实时异常和已经授权的单步动作。",
    task: ["scene_live", "检查实时漏斗异常", "异常时段、影响范围和下一次复查时间已记录"],
  },
  post_live: {
    label: "下播复盘",
    intro: "下播后同步最新数据，复盘调整效果并沉淀下一场任务。",
    task: ["scene_post_live", "完成下播数据复盘", "流量、点击、成交、ROI 和库存变化已有结论"],
  },
};

function localDateKey() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

function workbenchTasks() {
  const role = ROLE_WORKBENCH[currentRole] || ROLE_WORKBENCH["运营总管"];
  const scene = SCENE_WORKBENCH[workbenchScene] || SCENE_WORKBENCH.daily;
  return [scene.task, ...role.tasks.slice(0, 2)].map(([id, title, acceptance]) => ({ id, title, acceptance }));
}

function templateCheckKey(taskId) {
  return `${localDateKey()}:${workbenchScene}:${currentRole}:${taskId}`;
}

function renderWorkbench() {
  const role = ROLE_WORKBENCH[currentRole] || ROLE_WORKBENCH["运营总管"];
  const scene = SCENE_WORKBENCH[workbenchScene] || SCENE_WORKBENCH.daily;
  document.getElementById("workbench-title").textContent = role.title;
  document.getElementById("workbench-description").textContent = role.description;
  document.getElementById("workbench-scene").value = workbenchScene;
  document.getElementById("template-heading").textContent = `${scene.label} · 标准动作`;
  document.getElementById("template-intro").textContent = scene.intro;

  const tasks = workbenchTasks();
  const completed = tasks.filter((item) => templateChecks[templateCheckKey(item.id)]).length;
  document.getElementById("template-progress").textContent = `${completed}/${tasks.length}`;
  const container = document.getElementById("template-tasks");
  container.replaceChildren(...tasks.map((item) => {
    const done = Boolean(templateChecks[templateCheckKey(item.id)]);
    const card = document.createElement("article");
    card.className = `template-task${done ? " done" : ""}`;
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = done ? "✓" : "○";
    button.setAttribute("aria-label", done ? `重新打开：${item.title}` : `完成：${item.title}`);
    const body = document.createElement("div");
    const title = document.createElement("strong"); title.textContent = item.title;
    const acceptance = document.createElement("p"); acceptance.textContent = `验收：${item.acceptance}`;
    body.append(title, acceptance);
    button.addEventListener("click", async () => {
      const key = templateCheckKey(item.id);
      templateChecks[key] = !templateChecks[key];
      const today = `${localDateKey()}:`;
      templateChecks = Object.fromEntries(Object.entries(templateChecks).filter(([storedKey]) => storedKey.startsWith(today)));
      await chrome.storage.local.set({ templateChecks });
      renderWorkbench();
    });
    card.append(button, body);
    return card;
  }));
}

function appendCopyAction(card, params) {
  if (!params) return;
  const wrap = document.createElement("div");
  wrap.className = "action-params-wrap";
  const isDraft = Number(params.schema_version || 0) >= 1;
  const change = params.change || {};
  const target = params.target_ref || {};
  const field = change.field ?? params.field;
  const currentValue = change.current_value ?? params.current_value;
  const targetValue = change.target_value ?? params.target_value;
  const blockedReasons = Array.isArray(params.blocked_reasons) ? params.blocked_reasons : [];
  if (isDraft) {
    wrap.classList.add(params.can_confirm ? "confirmable" : "blocked");
    if (params.state === "confirmed") wrap.classList.add("confirmed");
  }
  if (params.operation_label) {
    const label = document.createElement("span");
    label.className = "action-label";
    label.textContent = params.operation_label;
    wrap.append(label);
  }
  if (field && (currentValue != null || targetValue != null)) {
    const strip = document.createElement("span");
    strip.className = "action-param-strip";
    const cur = currentValue != null ? String(currentValue) : "--";
    const tgt = targetValue != null ? String(targetValue) : "--";
    strip.textContent = currentValue != null && targetValue == null
      ? `${field}当前值 ${cur} · 目标值待确认`
      : currentValue == null && targetValue != null
        ? `${field}目标值 ${tgt}`
        : `${field} ${cur} → ${tgt}`;
    wrap.append(strip);
  }
  if (isDraft) {
    const identity = document.createElement("small");
    identity.className = "action-identity";
    const account = target.account_label || target.account_key || "账号未锁定";
    const planId = target.id ? `计划 ID ${target.id}` : "缺少计划 ID";
    identity.textContent = `${account} · ${planId}`;
    wrap.append(identity);

    const hint = document.createElement("small");
    hint.className = "action-state-hint";
    if (params.state === "confirmed") {
      hint.textContent = "已确认方案，尚未执行任何千川操作";
    } else if (params.state === "cancelled") {
      hint.textContent = "本次确认已撤销，未执行千川操作";
    } else if (blockedReasons.length) {
      hint.textContent = blockedReasons.map((item) => item.message).filter(Boolean).slice(0, 2).join("；");
    } else {
      hint.textContent = "确认只会写入本地记录，不会自动提交千川";
    }
    wrap.append(hint);
  }

  const buttons = document.createElement("div");
  buttons.className = "action-buttons";
  if (params.copy_text) {
    const btn = document.createElement("button");
    btn.className = "copy-action-btn";
    btn.textContent = "复制处理建议";
    btn.setAttribute("aria-label", "复制: " + params.copy_text);
    btn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(params.copy_text);
        const original = btn.textContent;
        btn.textContent = "已复制";
        btn.disabled = true;
        setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 1500);
      } catch {
        btn.textContent = "复制失败";
      }
    });
    buttons.append(btn);
  }
  if (isDraft) {
    const confirmButton = document.createElement("button");
    confirmButton.className = "confirm-action-btn";
    const confirmed = params.state === "confirmed";
    const cancelled = params.state === "cancelled";
    confirmButton.textContent = confirmed ? "撤销确认" : cancelled ? "已撤销" : params.can_confirm ? "确认方案" : "需补齐数据";
    confirmButton.disabled = cancelled || (!confirmed && !params.can_confirm);
    confirmButton.addEventListener("click", async () => {
      confirmButton.disabled = true;
      const hint = wrap.querySelector(".action-state-hint");
      const isConfirmedNow = params.state === "confirmed";
      try {
        const response = await bridgeFetch(isConfirmedNow ? "/actions/cancel" : "/actions/confirm", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
          body: JSON.stringify(isConfirmedNow ? { action_id: params.action_id } : { action: params }),
        });
        params.state = response.action?.state || (isConfirmedNow ? "cancelled" : "confirmed");
        if (hint) {
          hint.textContent = params.state === "confirmed"
            ? "已确认方案，尚未执行任何千川操作"
            : "本次确认已撤销，未执行千川操作";
        }
        confirmButton.textContent = params.state === "confirmed" ? "撤销确认" : "已撤销";
        confirmButton.disabled = params.state !== "confirmed";
        wrap.classList.toggle("confirmed", params.state === "confirmed");
        refreshAutomationReadiness().catch(() => undefined);
        refreshShadowExecution().catch(() => undefined);
      } catch (error) {
        if (hint) hint.textContent = `确认失败：${error.message}`;
        confirmButton.disabled = false;
      }
    });
    buttons.append(confirmButton);
  }
  if (buttons.childElementCount) wrap.append(buttons);
  card.append(wrap);
}

let selectedQianchuanAccount = "";
let accountSelectionRequired = false;

async function pollFullScan() {
  const response = await chrome.runtime.sendMessage({ type: "get-dashboard" });
  if (response?.ok) renderFullScan(response.dashboard?.fullScan || {});
}

async function bridgeFetch(path, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const buildHeaders = async (forceRefresh = false) => {
    const headers = { ...(options.headers || {}) };
    if (method === "GET" || method === "HEAD") return headers;
    if (forceRefresh) await chrome.storage.local.remove("bridgeToken");
    const stored = await chrome.storage.local.get("bridgeToken");
    let token = stored.bridgeToken;
    if (!token) {
      const bootstrap = await fetch(`${BRIDGE_URL}/auth/bootstrap`, {
        cache: "no-store",
        headers: { "X-Dian-Agent": "2" },
      });
      const bootstrapValue = await bootstrap.json().catch(() => ({}));
      if (!bootstrap.ok || !bootstrapValue.token) {
        throw new Error(bootstrapValue.error || "无法领取本地 Agent 写入令牌");
      }
      token = bootstrapValue.token;
      await chrome.storage.local.set({ bridgeToken: token });
    }
    headers["Content-Type"] = headers["Content-Type"] || "application/json";
    headers["X-Dian-Agent"] = headers["X-Dian-Agent"] || "2";
    headers.Authorization = `Bearer ${token}`;
    return headers;
  };
  let headers = await buildHeaders(false);
  let response = await fetch(`${BRIDGE_URL}${path}`, { cache: "no-store", ...options, headers });
  let value = await response.json().catch(() => ({}));
  if (response.status === 403 && value.error === "missing_or_invalid_bridge_token" && method !== "GET" && method !== "HEAD") {
    headers = await buildHeaders(true);
    response = await fetch(`${BRIDGE_URL}${path}`, { cache: "no-store", ...options, headers });
    value = await response.json().catch(() => ({}));
  }
  if (!response.ok) throw new Error(value.error || `本地 Agent 返回 HTTP ${response.status}`);
  return value;
}

function renderConnection(ok, title, detail) {
  const element = document.getElementById("connection");
  element.className = `connection ${ok ? "ok" : "error"}`;
  element.querySelector("strong").textContent = title;
  element.querySelector("p").textContent = detail;
}

function renderTrends(trends = {}) {
  const container = document.getElementById("trend-list");
  const changes = (trends.changes || []).filter((item) => item.points?.length >= 2).slice(0, 4);
  document.getElementById("trend-count").textContent = trends.history_points ? `${trends.history_points} 个历史点` : "积累中";
  if (!changes.length) return empty(container, "历史数据正在积累，完成两次不同时段巡检后开始展示变化");
  container.className = "trend-list";
  container.replaceChildren(...changes.map((item) => {
    const card = document.createElement("article");
    const heading = document.createElement("div"); heading.className = "trend-heading";
    const title = document.createElement("strong"); title.textContent = item.label;
    const delta = document.createElement("span");
    delta.textContent = item.delta_percent == null ? `${item.delta >= 0 ? "+" : ""}${item.delta.toFixed(1)}` : `${item.delta_percent >= 0 ? "+" : ""}${item.delta_percent.toFixed(1)}%`;
    delta.className = item.delta >= 0 ? "up" : "down";
    heading.append(title, delta);
    const bars = document.createElement("div"); bars.className = "spark-bars";
    const values = item.points.map((point) => point.value); const min = Math.min(...values); const max = Math.max(...values);
    item.points.slice(-12).forEach((point) => { const bar = document.createElement("span"); bar.style.height = `${20 + (max === min ? 40 : (point.value - min) / (max - min) * 80)}%`; bars.append(bar); });
    const detail = document.createElement("small"); detail.textContent = `${item.first.toLocaleString()} → ${item.last.toLocaleString()}`;
    card.append(heading, bars, detail); return card;
  }));
}

function empty(container, message) {
  container.className = "stack empty-state";
  container.textContent = message;
}

function setModuleActionCount(childId, count) {
  const section = document.getElementById(childId)?.closest(".module-section");
  if (section) section.dataset.actionCount = String(Math.max(0, Number(count || 0)));
}

function applyModuleVisibility() {
  const toolbar = document.getElementById("focus-toolbar");
  const toggle = document.getElementById("focus-toggle");
  const summary = document.getElementById("focus-summary");
  const managerView = currentRole === "运营总管";
  toolbar.hidden = !managerView;

  let roleModules = 0;
  let actionableModules = 0;
  let hiddenEmptyModules = 0;
  document.querySelectorAll(".module-section").forEach((section) => {
    const owners = String(section.dataset.owner || "").split(/\s+/).filter(Boolean);
    const belongsToRole = managerView || owners.includes(currentRole);
    const actionable = Number(section.dataset.actionCount || 0) > 0;
    if (belongsToRole) {
      roleModules += 1;
      if (actionable) actionableModules += 1;
    }
    const hideForFocus = managerView && focusOnlyActionable && !actionable;
    section.hidden = !belongsToRole || hideForFocus;
    if (belongsToRole && hideForFocus) hiddenEmptyModules += 1;
  });

  if (!managerView) return;
  toggle.setAttribute("aria-pressed", String(focusOnlyActionable));
  toolbar.classList.toggle("showing-all", !focusOnlyActionable);
  if (focusOnlyActionable) {
    toggle.textContent = hiddenEmptyModules ? `显示全部模块（${hiddenEmptyModules}）` : "已经显示全部";
    toggle.disabled = hiddenEmptyModules === 0;
    summary.textContent = actionableModules
      ? `已显示 ${actionableModules} 个有任务模块，隐藏 ${hiddenEmptyModules} 个暂无任务模块。`
      : "当前没有专项任务；可显示全部模块查看经营指标和数据状态。";
  } else {
    toggle.disabled = false;
    toggle.textContent = "只看需要处理";
    summary.textContent = `当前显示全部 ${roleModules} 个经营模块。`;
  }
}

function renderAlerts(alerts = []) {
  const container = document.getElementById("alerts");
  document.getElementById("alert-count").textContent = `${alerts.length} 项`;
  if (!alerts.length) return empty(container, "目前没有需要优先处理的其他异常");
  container.className = "stack";
  container.replaceChildren(...alerts.slice(0, 6).map((alert) => {
    const card = document.createElement("article");
    card.className = `alert-card ${alert.level || "info"}`;
    const icon = document.createElement("div");
    icon.className = "alert-icon";
    icon.textContent = alert.level === "high" ? "!" : alert.level === "warning" ? "△" : "i";
    const body = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = alert.title || "提示";
    const detail = document.createElement("p");
    detail.textContent = alert.action || alert.detail || "请回到后台核对。";
    body.append(title, detail);
    card.append(icon, body);
    return card;
  }));
}

function renderCoverage(coverage = []) {
  const container = document.getElementById("coverage");
  if (!coverage.length) {
    container.innerHTML = '<div class="empty-state">尚无页面快照</div>';
    return;
  }
  container.replaceChildren(...coverage.map((item) => {
    const card = document.createElement("article");
    card.className = "coverage-card";
    const title = document.createElement("strong");
    title.textContent = `${LABELS[item.source] || item.source} · ${LABELS[item.page_type] || item.page_type}`;
    const detail = document.createElement("p");
    detail.textContent = `${item.age_label || "已缓存"} · ${item.metric_count || 0} 指标 · ${item.row_count || 0} 行`;
    const score = document.createElement("div");
    score.className = "score";
    const bar = document.createElement("span");
    bar.style.width = `${Math.max(3, Math.min(100, item.quality_score || 0))}%`;
    score.append(bar);
    card.append(title, detail, score);
    return card;
  }));
}

function renderSettings(settings, extensionSettings = {}) {
  document.getElementById("execution-mode").value = settings.execution_mode || "observe";
  document.getElementById("roi-target").value = settings.roi_target;
  document.getElementById("spend-threshold").value = settings.min_spend_for_action;
  document.getElementById("stock-threshold").value = settings.low_inventory_threshold;
  document.getElementById("daily-execution-limit").value = settings.max_daily_execution_count ?? 3;
  document.getElementById("daily-budget-limit").value = settings.max_daily_budget_reduction ?? 300;
  document.getElementById("execution-cooldown").value = settings.execution_cooldown_minutes ?? 30;
  document.getElementById("max-deep-scan-pages").value = extensionSettings.maxDeepScanPages ?? 5;
  document.getElementById("report-time").value = settings.daily_report_time;
  document.getElementById("report-enabled").checked = settings.daily_report_enabled;
  const template = REPORT_TEMPLATE_LABELS[settings.report_template] ? settings.report_template : "default";
  document.getElementById("report-template").value = template;
  document.getElementById("custom-report-template").value = settings.custom_report_template || "";
  document.getElementById("custom-template-wrap").hidden = template !== "custom";
  document.getElementById("report-template-label").textContent = REPORT_TEMPLATE_LABELS[template];
}

function renderIntegrations(payload = {}) {
  const platforms = ["feishu", "dingtalk"];
  let connected = 0;
  platforms.forEach((platform) => {
    const item = payload[platform] || {};
    const state = document.getElementById(`${platform}-state`);
    state.textContent = item.configured ? "已连接" : "未连接";
    state.className = item.configured ? "connected" : "";
    const input = document.getElementById(`${platform}-webhook`);
    input.value = "";
    input.placeholder = item.configured
      ? "已安全保存在本机；留空表示不修改"
      : platform === "feishu"
        ? "https://open.feishu.cn/open-apis/bot/v2/hook/..."
        : "https://oapi.dingtalk.com/robot/send?access_token=...";
    if (item.configured) connected += 1;
  });
  document.getElementById("auto-send-reports").checked = Boolean(payload.auto_send_reports);
  document.getElementById("integration-status").textContent = connected ? `已连接 ${connected} 个平台` : "未连接";
}

function renderOceanEngineOAuth(payload = {}) {
  const state = document.getElementById("oceanengine-oauth-state");
  const result = document.getElementById("oceanengine-oauth-result");
  const appId = document.getElementById("oceanengine-app-id");
  const secret = document.getElementById("oceanengine-app-secret");
  const accountBox = document.getElementById("oceanengine-accounts");
  const accounts = Array.isArray(payload.accounts) ? payload.accounts : [];
  document.getElementById("sync-oceanengine-data").disabled = !payload.connected;
  if (payload.app_id) appId.value = payload.app_id;
  secret.value = "";
  secret.placeholder = payload.secret_saved
    ? "已用 Windows 本机加密保存；留空继续使用"
    : "从开放平台复制；仅加密保存在本机";
  state.className = payload.connected ? "connected" : payload.authorization_in_progress ? "waiting" : "";
  state.textContent = payload.connected
    ? "已连接"
    : payload.authorization_in_progress
      ? "等待授权完成"
      : payload.secret_saved
        ? "待授权账号"
        : "尚未连接";
  document.getElementById("oceanengine-account-count").textContent = `${Number(payload.account_count || 0)} 个账号`;
  accountBox.replaceChildren(...accounts.map((account) => {
    const chip = document.createElement("span");
    chip.textContent = account.account_name || `千川账号 ${account.account_id || ""}`;
    return chip;
  }));
  accountBox.hidden = !accounts.length;
  if (payload.connected) {
    result.textContent = accounts.length
      ? `官方 API 已连接，已识别 ${accounts.length} 个授权账号。`
      : "官方 API 已连接；账号名称会在首次 API 同步后补齐。";
    result.className = "ok";
  } else if (payload.last_error) {
    result.textContent = payload.last_error;
    result.className = "error";
  } else if (payload.authorization_in_progress) {
    result.textContent = "授权页面已打开，请选择要授权的千川账号并确认；完成后这里会自动更新。";
    result.className = "";
  } else {
    result.textContent = payload.secret_saved
      ? "App Secret 已安全保存在本机，现在可以授权千川账号。"
      : "第一次填写 App Secret；以后新增账号直接点“授权千川账号”。密钥和 Token 不上传到 GitHub。";
    result.className = "";
  }
}

function renderOceanEngineSync(payload = {}) {
  const result = document.getElementById("oceanengine-sync-result");
  if (!payload.synced_at) {
    result.textContent = "同步会自动读取已授权店铺关联的广告账户、计划、7 日经营报表和视频素材；不会修改投放。";
    result.className = "";
    return;
  }
  const time = new Date(Number(payload.synced_at) * 1000).toLocaleString("zh-CN", { hour12: false });
  const failures = Number(payload.failure_count || 0);
  result.textContent = failures
    ? `上次同步 ${time}：${payload.account_count || 0} 个店铺、保存 ${payload.saved_pages || 0} 类数据；${failures} 个接口未获权限或读取失败，浏览器快照仍作为备用。`
    : `上次同步 ${time}：${payload.account_count || 0} 个店铺、保存 ${payload.saved_pages || 0} 类数据，全部只读接口成功。`;
  result.className = failures ? "warn" : "ok";
}

async function refreshOceanEngineStatus() {
  const [status, sync] = await Promise.all([
    bridgeFetch("/oauth/oceanengine/status"),
    bridgeFetch("/oauth/oceanengine/sync-status"),
  ]);
  renderOceanEngineOAuth(status);
  renderOceanEngineSync(sync);
  if (status.connected && oceanengineStatusPoller) {
    clearInterval(oceanengineStatusPoller);
    oceanengineStatusPoller = null;
  }
  return status;
}

function startOceanEngineStatusPolling() {
  if (oceanengineStatusPoller) clearInterval(oceanengineStatusPoller);
  let attempts = 0;
  oceanengineStatusPoller = setInterval(async () => {
    attempts += 1;
    try {
      const status = await refreshOceanEngineStatus();
      if (status.connected || !status.authorization_in_progress || attempts >= 90) {
        clearInterval(oceanengineStatusPoller);
        oceanengineStatusPoller = null;
      }
    } catch {
      if (attempts >= 90) {
        clearInterval(oceanengineStatusPoller);
        oceanengineStatusPoller = null;
      }
    }
  }, 2000);
}

function renderQianchuanAccounts(payload = {}) {
  const select = document.getElementById("qianchuan-account-select");
  const accounts = payload.stores || payload.accounts || [];
  const analysisAccountKey = String(payload.selected_account_key || "");
  // The Agent setting is the only source of truth. Never silently fall back to
  // another store because a stale browser preference can mix operational context.
  const activeKey = accounts.some((account) => account.key === analysisAccountKey)
    ? analysisAccountKey
    : "";
  const analysisAccount = accounts.find((account) => account.key === activeKey);
  select.replaceChildren();
  const current = document.createElement("option");
  current.value = "";
  current.textContent = accounts.length ? "请选择当前店铺" : "尚未识别店铺";
  select.append(current);
  const labelCounts = accounts.reduce((counts, account) => {
    const label = String(account.label || "未命名账号");
    counts.set(label, (counts.get(label) || 0) + 1);
    return counts;
  }, new Map());
  accounts.forEach((account) => {
    const option = document.createElement("option");
    option.value = account.key;
    const duplicate = (labelCounts.get(String(account.label || "未命名账号")) || 0) > 1;
    const suffix = String(account.key || "").slice(-4).toUpperCase();
    const accountLabel = duplicate ? `${account.label} · ${suffix}` : account.label;
    const stateLabel = account.state_label || (account.channel === "official_api" ? "官方 API" : "网页");
    option.textContent = `${accountLabel} · ${stateLabel}`;
    select.append(option);
  });
  selectedQianchuanAccount = activeKey;
  select.value = activeKey;
  chrome.storage.local.set({ scanAccountPreference: activeKey });
  accountSelectionRequired = !activeKey;
  const scanButton = document.getElementById("full-scan-button");
  scanButton.disabled = !activeKey;
  scanButton.title = activeKey ? "按当前店铺开始巡检" : "请先选择当前店铺";
  document.getElementById("active-store-name").textContent = analysisAccount?.label || "尚未识别店铺";
  document.getElementById("store-mode-summary").textContent = analysisAccount
    ? `${payload.store_count || accounts.length} 个店铺 · 当前${analysisAccount.state_label || "数据已隔离"} · 建议与日志仅使用本店数据`
    : "授权官方 API 或绑定一个已登录千川页面后开始。";
  document.getElementById("qianchuan-account-hint").textContent = analysisAccount
    ? `当前巡检固定为“${analysisAccount.label}”；不会读取或混合其他店铺数据。`
    : "尚未选择店铺，本次不会启动千川巡检。";
}

function renderOperationContext(payload = {}) {
  currentOperationContext = payload;
  const container = document.getElementById("operation-context");
  const state = ["ready", "review", "blocked"].includes(payload.state) ? payload.state : "checking";
  container.className = `operation-context ${state}`;
  document.getElementById("context-state").textContent = payload.state_label || "正在核对";
  document.getElementById("context-store").textContent = payload.selected_store?.label || "尚未选择";
  document.getElementById("context-source").textContent = payload.source_label || "尚未绑定";
  document.getElementById("context-freshness").textContent = payload.freshness?.label || "暂无更新时间";
  const coverage = payload.coverage || {};
  document.getElementById("context-coverage").textContent = coverage.official_ready
    ? "官方数据可用"
    : coverage.label || "尚未体检";
  document.getElementById("context-next-action").textContent = payload.next_action || "先补齐数据，再处理今日任务。";
  const warnings = [...(payload.blockers || []), ...(payload.warnings || [])].slice(0, 4);
  const warningBox = document.getElementById("context-warnings");
  warningBox.hidden = warnings.length === 0;
  warningBox.replaceChildren(...warnings.map((message) => {
    const item = document.createElement("span");
    item.textContent = message;
    return item;
  }));
  const action = document.getElementById("context-action");
  action.textContent = state === "ready" ? "查看数据体检" : payload.selected_store?.key ? "补齐经营数据" : "选择并绑定店铺";
}

function renderHealthMonitor(health = {}) {
  const alerts = health.alerts || [];
  const container = document.getElementById("health-alerts");
  const baselines = health.baselines || {};
  const trackedCount = Object.keys(baselines).length;
  document.getElementById("health-count").textContent = alerts.length ? `${alerts.length} 项异常` : `${trackedCount} 页已监测`;
  if (!alerts.length) {
    if (trackedCount > 0) {
      container.className = "stack";
      container.textContent = `已跟踪 ${trackedCount} 个页面采集质量，${health.pages_with_baseline || 0} 个已建立基线。当前无异常。`;
    }
    return;
  }
  container.className = "stack";
  container.replaceChildren(...alerts.map((alert) => {
    const card = document.createElement("article");
    card.className = `alert-card ${alert.level || "info"}`;
    const icon = document.createElement("div"); icon.className = "alert-icon";
    icon.textContent = alert.level === "high" ? "!" : "△";
    const body = document.createElement("div");
    const title = document.createElement("strong"); title.textContent = alert.title;
    const detail = document.createElement("p"); detail.textContent = alert.detail;
    const action = document.createElement("small"); action.textContent = alert.action;
    body.append(title, detail, action);
    card.append(icon, body);
    return card;
  }));
}

function renderEffectiveness(report = {}) {
  const rate = report.effective_rate;
  const rateEl = document.getElementById("effectiveness-rate");
  const container = document.getElementById("effectiveness-detail");
  if (report.total_evaluated === 0) {
    rateEl.textContent = "暂无数据";
    container.className = "stack empty-state";
    container.textContent = '完成任务（标记为“已完成”）后自动对比前后指标，评估建议有效性';
    return;
  }
  rateEl.textContent = `${rate}% 有效`;
  container.className = "stack";
  const summary = document.createElement("p");
  summary.textContent = `共评估 ${report.total_evaluated} 条已完成建议，其中 ${report.effective_count} 条有效（${rate}%）。`;
  container.replaceChildren(summary);
  const recent = report.recent_evaluations || [];
  if (recent.length) {
    const heading = document.createElement("small"); heading.textContent = "最近评估：";
    container.append(heading);
    recent.slice(0, 5).forEach((item) => {
      const row = document.createElement("div"); row.className = "eval-row";
      const tag = document.createElement("span");
      tag.textContent = item.effective ? "有效" : "无效";
      tag.className = `eval-tag ${item.effective ? "ok" : "warn"}`;
      const detail = document.createElement("small");
      const changes = (item.changes || []).slice(0, 2).map((change) => `${change.metric}: ${change.old}→${change.new}`).join(", ");
      detail.textContent = changes || "指标变化不明显";
      row.append(tag, detail);
      container.append(row);
    });
  }
}

function renderAutomationReadiness(report = {}) {
  const summary = report.summary || {};
  const items = report.items || [];
  const totalActionable = Number(summary.preflight_ready || 0) + Number(summary.confirmable || 0) + Number(summary.blocked || 0);
  setModuleActionCount("automation-candidates", items.length);
  applyModuleVisibility();
  document.getElementById("automation-status").textContent = summary.preflight_ready
    ? `${summary.preflight_ready} 项可进入执行前检查`
    : summary.confirmable
      ? `${summary.confirmable} 项等待授权`
      : items.length ? "条件待补齐" : "等待千川数据";
  renderMetricStrip("automation-summary", {
    可进入检查: summary.preflight_ready || 0,
    等待授权: summary.confirmable || 0,
    暂时阻止: summary.blocked || 0,
    仅人工处理: summary.manual_only || 0,
  });

  const criteria = document.getElementById("automation-criteria-list");
  criteria.replaceChildren(...(report.criteria || []).map((value) => {
    const item = document.createElement("li");
    item.textContent = value;
    return item;
  }));

  const container = document.getElementById("automation-candidates");
  if (!items.length) return empty(container, "同步千川计划后，这里会显示哪些动作可授权、哪些被阻止以及下一步怎么补齐。");
  container.className = "stack";
  container.replaceChildren(...items.slice(0, 10).map((item) => {
    const card = document.createElement("article");
    card.className = `automation-candidate ${item.status || "blocked"}`;
    const header = document.createElement("header");
    const title = document.createElement("strong"); title.textContent = item.plan || "千川计划";
    const tag = document.createElement("span"); tag.className = "automation-ready-tag"; tag.textContent = item.status_label || "待检查";
    header.append(title, tag);
    const change = document.createElement("p"); change.className = "automation-change";
    change.textContent = item.field
      ? `${item.field}  ${item.current_value ?? "--"} → ${item.target_value ?? "--"}`
      : item.operation_label || "人工运营建议";
    const next = document.createElement("p"); next.className = "automation-next";
    next.textContent = `下一步：${item.next_step || "返回千川计划核对数据。"}`;
    card.append(header, change, next);

    const blockedReasons = (item.blocked_reasons || []).map((reason) => reason.message).filter(Boolean);
    if (blockedReasons.length) {
      const blockers = document.createElement("div");
      blockers.className = "automation-blockers";
      blockers.textContent = blockedReasons.slice(0, 2).join("；");
      card.append(blockers);
    }

    if (item.status !== "manual_only") {
      const actions = document.createElement("div"); actions.className = "automation-card-actions";
      const button = document.createElement("button"); button.type = "button";
      const needsReread = (item.blocked_reasons || []).some((reason) => ["DATA_STALE", "CAPTURE_TIME_MISSING", "DATA_QUALITY_LOW", "SNAPSHOT_TRUNCATED", "CONFIDENCE_NOT_HIGH"].includes(reason.code));
      button.textContent = needsReread ? "补采当前千川页" : item.status === "confirmable" ? "查看并确认方案" : item.status === "preflight_ready" ? "启动执行前检查" : "查看投放方案";
      button.addEventListener("click", async () => {
        if (needsReread) {
          document.getElementById("current-qianchuan-button").click();
          document.getElementById("scan-receipt")?.scrollIntoView({ behavior: "smooth", block: "start" })
            || document.querySelector(".scan-card")?.scrollIntoView({ behavior: "smooth", block: "start" });
          return;
        }
        if (item.status === "preflight_ready" && item.action_id) {
          button.disabled = true;
          button.textContent = "正在启动检查…";
          try {
            const response = await bridgeFetch("/actions/preflight/start", {
              method: "POST",
              headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
              body: JSON.stringify({ action_id: item.action_id }),
            });
            renderExecutionPreflight(response.preflight || {});
            document.getElementById("execution-preflight").scrollIntoView({ behavior: "smooth", block: "center" });
          } catch (error) {
            button.textContent = error.message || "启动失败";
            button.disabled = false;
          }
          return;
        }
        revealModuleByChildId("plans");
      });
      actions.append(button);
      card.append(actions);
    }
    return card;
  }));
  if (!totalActionable) document.getElementById("automation-status").textContent = "仅保留人工建议";
}

function renderExecutionPreflight(report = {}) {
  const panel = document.getElementById("execution-preflight");
  const state = report.state || "idle";
  currentPreflightSession = report.session || null;
  const stages = [...document.querySelectorAll("[data-automation-stage]")];
  stages.forEach((stage) => stage.classList.remove("done", "current"));
  const stageMap = Object.fromEntries(stages.map((stage) => [stage.dataset.automationStage, stage]));
  stageMap.diagnosis?.classList.add("done");
  if (state === "idle") {
    stageMap.qualification?.classList.add("current");
  } else {
    stageMap.qualification?.classList.add("done");
    stageMap.authorization?.classList.add("done");
    stageMap.preflight?.classList.add("current");
  }
  panel.hidden = state === "idle";
  panel.className = `execution-preflight${state === "ready_for_final_confirmation" ? " ready" : state === "blocked" || state === "expired" ? " blocked" : state === "stopped" ? " stopped" : ""}`;
  document.getElementById("preflight-state").textContent = report.state_label || "尚未启动";
  const action = report.action || {};
  document.getElementById("preflight-target").textContent = action.plan_name
    ? `${action.account_label || "千川账号"} · ${action.plan_name} · ${action.field || "预算"} ${action.current_value ?? "--"} → ${action.target_value ?? "--"}`
    : "等待选择已授权的止损方案";
  const impact = action.impact_preview || {};
  if (action.plan_name && impact.change_percent != null) {
    document.getElementById("preflight-target").textContent +=
      ` · 影响 ${impact.change_percent}% · 今日消耗 ¥${impact.today_spend ?? "--"} · 单日额度 ¥${impact.daily_budget_impact_limit ?? "--"} · 回滚条件：${impact.rollback_condition}`;
  }
  const checks = document.getElementById("preflight-checks");
  checks.replaceChildren(...(report.checks || []).map((item) => {
    const row = document.createElement("div"); row.className = `preflight-check${item.passed ? " passed" : ""}`;
    const mark = document.createElement("span"); mark.textContent = item.passed ? "✓" : "!";
    const body = document.createElement("div");
    const title = document.createElement("strong"); title.textContent = item.label;
    const detail = document.createElement("small"); detail.textContent = item.detail || "";
    body.append(title, detail); row.append(mark, body);
    return row;
  }));
  document.getElementById("preflight-next").textContent = report.next_step || "首批只检查降低预算动作，不开放自动放量。";
  const reread = document.getElementById("preflight-reread");
  const authorize = document.getElementById("preflight-authorize");
  const stop = document.getElementById("preflight-stop");
  reread.hidden = ["ready_for_final_confirmation", "authorized", "stopped"].includes(state);
  reread.disabled = state === "expired";
  authorize.hidden = state !== "ready_for_final_confirmation";
  authorize.dataset.targetValue = action.target_value ?? "";
  authorize.dataset.operationType = action.operation_type || "adjust_budget";
  stop.disabled = !currentPreflightSession || ["stopped", "expired"].includes(state);
}

function renderExecutionEffectiveness(report = {}) {
  const items = report.items || [];
  const summary = report.summary || {};
  document.getElementById("execution-effectiveness-status").textContent = `${items.length} 项`;
  const container = document.getElementById("execution-effectiveness");
  if (!items.length) return empty(container, "完成一次受监督执行后，这里会按消耗速度生成动态复查任务。");
  container.classList.remove("empty-state");
  container.replaceChildren(...items.map((item) => {
    const card = document.createElement("article");
    card.className = `shadow-card${item.status === "effective" ? " matched" : item.status === "review" ? " attention" : ""}`;
    const head = document.createElement("div"); head.className = "card-title";
    const title = document.createElement("strong"); title.textContent = item.plan_name || "千川计划";
    const tag = document.createElement("span"); tag.className = "shadow-status"; tag.textContent = item.status_label || "待复查";
    head.append(title, tag);
    const change = document.createElement("p"); change.className = "shadow-change";
    change.textContent = `预算 ${item.change?.from ?? "--"} → ${item.change?.to ?? "--"}`;
    const metrics = document.createElement("small");
    metrics.textContent = item.after
      ? `调整前 ROI ${item.before?.roi ?? "--"} / 订单 ${item.before?.orders ?? "--"}；最新 ROI ${item.after?.roi ?? "--"} / 订单 ${item.after?.orders ?? "--"}`
      : "等待新的计划数据";
    const verdict = document.createElement("p"); verdict.className = "shadow-detail"; verdict.textContent = item.verdict || "";
    const windowHint = document.createElement("small");
    const minutes = item.observation_window_minutes || 120;
    windowHint.textContent = `复查窗口：${minutes >= 60 ? `${minutes / 60} 小时` : `${minutes} 分钟`}`;
    card.append(head, change, metrics, windowHint, verdict);
    if (item.rollback_available) {
      const rollback = document.createElement("button");
      rollback.type = "button";
      rollback.textContent = "生成恢复原预算方案";
      rollback.addEventListener("click", async () => {
        if (!window.confirm(`确认基于已验收记录，为“${item.plan_name || "该计划"}”生成恢复原预算方案？生成后仍需最终口令授权。`)) return;
        rollback.disabled = true;
        try {
          const created = await bridgeFetch("/actions/rollback/create", {
            method: "POST",
            body: JSON.stringify({ action_id: item.action_id }),
          });
          const confirmed = await bridgeFetch("/actions/confirm", {
            method: "POST",
            body: JSON.stringify({ action: created.action }),
          });
          const started = await bridgeFetch("/actions/preflight/start", {
            method: "POST",
            body: JSON.stringify({ action_id: confirmed.action.action_id }),
          });
          renderExecutionPreflight(started.preflight || {});
          document.getElementById("execution-preflight").scrollIntoView({ behavior: "smooth", block: "center" });
        } catch (error) {
          verdict.textContent = error.message || "回滚方案生成失败";
          rollback.disabled = false;
        }
      });
      card.append(rollback);
    }
    return card;
  }));
  document.getElementById("execution-effectiveness-status").textContent =
    summary.review ? `${summary.review} 项需复核` : summary.needs_reread ? `${summary.needs_reread} 项待读取` : `${items.length} 项`;
}

function renderShadowExecution(report = {}) {
  const items = report.items || [];
  const summary = report.summary || {};
  const pendingItems = Number(summary.awaiting_manual_action || 0) + Number(summary.awaiting_readback || 0) + Number(summary.needs_attention || 0);
  setModuleActionCount("shadow-actions", pendingItems || (Object.keys(summary).length ? 0 : items.length));
  applyModuleVisibility();
  document.getElementById("shadow-count").textContent = `${items.length} 项`;
  renderMetricStrip("shadow-summary", {
    待人工执行: summary.awaiting_manual_action || 0,
    待重新读取: summary.awaiting_readback || 0,
    已匹配: summary.matched || 0,
    需核对: summary.needs_attention || 0,
  });
  const container = document.getElementById("shadow-actions");
  if (!items.length) return empty(container, "当前没有待核验操作。确认调整方案后，这里会提示下一步。");
  container.className = "stack";
  container.replaceChildren(...items.slice(0, 10).map((item) => {
    const card = document.createElement("article");
    const attention = ["not_changed", "changed_differently", "unverifiable"].includes(item.status);
    card.className = `shadow-card${item.status === "matched" ? " matched" : attention ? " attention" : ""}`;
    const header = document.createElement("header");
    const title = document.createElement("strong"); title.textContent = item.plan_name || item.plan_id || "千川计划";
    const status = document.createElement("span"); status.className = "shadow-status"; status.textContent = item.status_label || "待处理";
    header.append(title, status);
    const change = document.createElement("p"); change.className = "shadow-change";
    change.textContent = `${item.field || "预算"}  ${item.before_value ?? "--"} → ${item.target_value ?? "--"}`;
    const detail = document.createElement("p"); detail.className = "shadow-detail"; detail.textContent = item.detail || "";
    card.append(header, change, detail);

    if (item.status !== "matched") {
      const footer = document.createElement("footer");
      if (item.status === "awaiting_manual_action") {
        const applied = document.createElement("button");
        applied.type = "button";
        applied.className = "primary";
        applied.textContent = "我已在千川手动执行";
        applied.addEventListener("click", async () => {
          applied.disabled = true;
          applied.textContent = "正在记录…";
          try {
            await bridgeFetch("/actions/manual-applied", {
              method: "POST",
              headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
              body: JSON.stringify({ action_id: item.action_id }),
            });
            await refreshShadowExecution();
          } catch (error) {
            applied.disabled = false;
            applied.textContent = error.message || "记录失败";
          }
        });
        footer.append(applied);
      } else {
        const reread = document.createElement("button");
        reread.type = "button";
        reread.className = "primary";
        reread.textContent = "读取当前千川页面并核验";
        reread.addEventListener("click", async () => {
          reread.disabled = true;
          reread.textContent = "正在读取…";
          try {
            const response = await chrome.runtime.sendMessage({ type: "sync-current-qianchuan" });
            if (!response?.ok) throw new Error(response?.error || "读取失败");
            await loadDashboard();
          } catch (error) {
            reread.disabled = false;
            reread.textContent = error.message || "读取失败";
          }
        });
        footer.append(reread);
      }
      card.append(footer);
    }
    return card;
  }));
}

async function refreshShadowExecution() {
  const report = await bridgeFetch("/actions/shadow");
  renderShadowExecution(report);
}

async function refreshAutomationReadiness() {
  const report = await bridgeFetch("/actions/readiness");
  renderAutomationReadiness(report);
}

async function refreshExecutionPreflight() {
  const report = await bridgeFetch("/actions/preflight");
  renderExecutionPreflight(report);
}

async function loadDashboard() {
  // Show loading skeleton
  showLoadingSkeleton();
  // Remember focus before re-render
  const focusedEl = document.activeElement;
  const focusId = focusedEl?.id || focusedEl?.closest("[id]")?.id;

  const [
    insightsR, actionCenterR, settingsR, opsR, extensionR, trendsR, accountsR, contextR, healthR, effectivenessR, readinessR, stopLossR, preflightR, shadowR, executionEffectivenessR, integrationsR, oceanengineR, oceanengineSyncR
  ] = await Promise.allSettled([
    bridgeFetch("/insights"),
    bridgeFetch("/action-center"),
    bridgeFetch("/settings"),
    bridgeFetch("/ops-manager"),
    chrome.runtime.sendMessage({ type: "get-dashboard" }),
    bridgeFetch("/trends?days=7"),
    bridgeFetch("/qianchuan-accounts"),
    bridgeFetch("/operation-context"),
    bridgeFetch("/health-monitor"),
    bridgeFetch("/effectiveness"),
    bridgeFetch("/actions/readiness"),
    bridgeFetch("/actions/stop-loss-queue"),
    bridgeFetch("/actions/preflight"),
    bridgeFetch("/actions/shadow"),
    bridgeFetch("/actions/effectiveness"),
    bridgeFetch("/integrations"),
    bridgeFetch("/oauth/oceanengine/status"),
    bridgeFetch("/oauth/oceanengine/sync-status"),
  ]);

  const val = (r, fallback) => r.status === "fulfilled" ? r.value : fallback;
  const insights = val(insightsR, { coverage: [], alerts: [], headline: "数据加载异常", summary: "部分模块连接失败，请稍后重试" });
  const actionCenter = val(actionCenterR, { plan_recommendations: [], inventory_alerts: [], shelf_analysis: {}, live_analysis: {}, creative_analysis: {} });
  const settings = val(settingsR, { roi_target: 1.5, min_spend_for_action: 100, low_inventory_threshold: 10, daily_report_time: "09:00", daily_report_enabled: true });
  const ops = val(opsR, { all_tasks: [], today_top_actions: [] });
  const extensionResponse = val(extensionR, {});
  const trends = val(trendsR, {});
  const accounts = val(accountsR, { accounts: [], selected_account_key: "" });
  const operationContext = val(contextR, { state: "blocked", state_label: "经营上下文读取失败", blockers: ["请刷新后重试"] });
  const health = val(healthR, {});
  const effectiveness = val(effectivenessR, {});
  const readiness = val(readinessR, { items: [], summary: {}, criteria: [] });
  const stopLoss = val(stopLossR, { items: [], summary: {} });
  const preflight = val(preflightR, { state: "idle", session: null, checks: [] });
  const shadow = val(shadowR, { items: [], summary: {} });
  const executionEffectiveness = val(executionEffectivenessR, { items: [], summary: {} });
  const integrations = val(integrationsR, { feishu: { configured: false }, dingtalk: { configured: false }, auto_send_reports: false });
  const oceanengine = val(oceanengineR, { app_id: "1871942906223351", connected: false, secret_saved: false, accounts: [] });
  const oceanengineSync = val(oceanengineSyncR, { synced_at: null });

  // Hide loading skeleton
  hideLoadingSkeleton();

  // Check for total failure (insights failed = bridge likely down)
  if (insightsR.status === "rejected") {
    renderConnection(false, "本地 Agent 未启动", "首次使用请双击 bridge/enable_autostart.bat");
    document.getElementById("headline").textContent = "暂时无法连接";
    document.getElementById("summary").textContent = insightsR.reason?.message || "请确认本地服务已启动";
    return;
  }

  renderConnection(true, "本地 Agent 已连接", `已读取 ${insights.coverage?.length || 0} 类页面快照`);
  document.getElementById("headline").textContent = insights.headline || "经营数据已同步";
  document.getElementById("summary").textContent = insights.summary || "请查看下方建议。";
  renderOperationContext(operationContext);
  renderPlans(actionCenter.plan_recommendations || []);
  renderInventory(actionCenter.inventory_alerts || []);
  renderOperations(ops, actionCenter.shelf_analysis || {}, actionCenter.live_analysis || {}, actionCenter.creative_analysis || {}, insights.coverage || []);
  renderAlerts(insights.alerts || []);
  renderCoverage(insights.coverage || []);
  renderSettings(settings, extensionResponse.dashboard?.settings || {});
  renderIntegrations(integrations);
  renderOceanEngineOAuth(oceanengine);
  renderOceanEngineSync(oceanengineSync);
  renderQianchuanAccounts(accounts);
  renderFullScan(extensionResponse?.dashboard?.fullScan || {});
  renderTrends(trends);
  renderHealthMonitor(health);
  renderEffectiveness(effectiveness);
  renderAutomationReadiness(readiness);
  renderStopLossQueue(stopLoss);
  renderExecutionPreflight(preflight);
  renderShadowExecution(shadow);
  renderExecutionEffectiveness(executionEffectiveness);

  // Restore focus if the focused element still exists
  if (focusId) {
    const restored = document.getElementById(focusId);
    if (restored) restored.focus();
  }

  latestBrief = [
    insights.headline, insights.summary,
    ...(ops.today_top_actions || []).slice(0, 8).map((item, index) => `总管 ${index + 1}. [${item.owner}] ${item.title}：${item.action}`),
    ...(actionCenter.plan_recommendations || []).slice(0, 5).map((item, index) => `千川 ${index + 1}. ${item.plan}：${item.suggestion}`),
    ...(actionCenter.creative_analysis?.recommendations || []).slice(0, 5).map((item, index) => `素材 ${index + 1}. ${item.title}：${item.action}`),
    ...(actionCenter.inventory_alerts || []).slice(0, 5).map((item, index) => `库存 ${index + 1}. ${item.product}：${item.suggestion}`),
    ...(shadow.items || []).slice(0, 5).map((item, index) => `影子执行 ${index + 1}. ${item.plan_name}：${item.status_label}`),
  ].filter(Boolean).join("\n");
}

function showLoadingSkeleton() {
  const shell = document.querySelector(".panel-shell");
  if (!shell || shell.querySelector(".loading-skeleton")) return;
  const skeleton = document.createElement("div");
  skeleton.className = "loading-skeleton";
  skeleton.setAttribute("aria-label", "正在加载数据");
  skeleton.innerHTML = '<div class="sk-bar"></div><div class="sk-bar short"></div><div class="sk-bar"></div>';
  shell.insertBefore(skeleton, shell.children[2]);
}

function hideLoadingSkeleton() {
  document.querySelectorAll(".loading-skeleton").forEach((el) => el.remove());
}

async function refreshAll(syncFirst = false) {
  const button = document.getElementById("sync-diagnose");
  if (syncFirst) {
    button.disabled = true;
    button.textContent = "正在同步…";
    await chrome.runtime.sendMessage({ type: "manual-sync" });
    await new Promise((resolve) => setTimeout(resolve, 800));
  }
  try {
    await loadDashboard();
  } catch (error) {
    renderConnection(false, "本地 Agent 未启动", "首次使用请双击 bridge/enable_autostart.bat");
    document.getElementById("headline").textContent = "暂时无法生成简报";
    document.getElementById("summary").textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "同步并诊断";
  }
}

function shortSyncTime(timestamp) {
  if (!timestamp) return "";
  return new Date(timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function setQianchuanSyncUi(state = "idle", detail = "读取最近页面") {
  const dock = document.getElementById("qianchuan-sync-dock");
  const dockButton = document.getElementById("qianchuan-sync-dock-button");
  const mainButton = document.getElementById("current-qianchuan-button");
  const dockLabel = document.getElementById("qianchuan-sync-dock-label");
  const dockStatus = document.getElementById("qianchuan-sync-dock-status");
  const running = state === "syncing";
  dock.className = `qianchuan-sync-dock ${state}`;
  dockButton.disabled = running;
  mainButton.disabled = running;
  dockLabel.textContent = running ? "同步中" : state === "success" ? "已同步" : state === "error" ? "同步失败" : "同步千川";
  dockStatus.textContent = detail;
  dockButton.title = detail;
  mainButton.textContent = running ? "正在同步千川页面…" : "同步最近千川页面";
}

function restoreQianchuanSyncUi(record = {}) {
  if (record.status === "success") {
    const label = record.account_label || LABELS[record.page_type] || "千川页面";
    setQianchuanSyncUi("success", `${label} · ${shortSyncTime(record.timestamp)}`);
  } else if (record.status === "error") {
    setQianchuanSyncUi("error", record.message || "请先打开千川");
  } else {
    setQianchuanSyncUi("idle", "读取最近页面");
  }
}

async function syncRecentQianchuanPage() {
  if (qianchuanSyncPromise) return qianchuanSyncPromise;
  qianchuanSyncPromise = (async () => {
    const hint = document.getElementById("qianchuan-account-hint");
    setQianchuanSyncUi("syncing", "正在读取最近页面");
    try {
      selectedQianchuanAccount = "";
      accountSelectionRequired = false;
      document.getElementById("qianchuan-account-select").value = "";
      await chrome.storage.local.set({ scanAccountPreference: "" });
      await bridgeFetch("/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
        body: JSON.stringify({ qianchuan_account_key: "" }),
      });
      const response = await chrome.runtime.sendMessage({ type: "sync-current-qianchuan" });
      if (!response?.ok) throw new Error(response?.error || "同步失败");
      const result = response.result || {};
      const accountLabel = result.account?.label || "";
      const pageLabel = LABELS[result.page_type] || result.page_type || "千川页面";
      const timestamp = Date.now();
      const record = {
        status: "success",
        timestamp,
        account_label: accountLabel,
        page_type: result.page_type || "",
        tab_id: result.tab?.id || null,
      };
      await chrome.storage.local.set({ lastQianchuanManualSync: record });
      await loadDashboard();
      hint.textContent = `已同步最近访问的${pageLabel}${accountLabel ? ` · ${accountLabel}` : ""}。后续巡检会优先复用这个千川标签页。`;
      setQianchuanSyncUi("success", `${accountLabel || pageLabel} · ${shortSyncTime(timestamp)}`);
      return result;
    } catch (error) {
      const message = error.message || "同步失败，请先打开巨量千川页面";
      await chrome.storage.local.set({ lastQianchuanManualSync: { status: "error", timestamp: Date.now(), message } });
      hint.textContent = message;
      setQianchuanSyncUi("error", message);
      throw error;
    } finally {
      qianchuanSyncPromise = null;
    }
  })();
  return qianchuanSyncPromise;
}

document.addEventListener("DOMContentLoaded", async () => {
  const stored = await chrome.storage.local.get(["preferredRole", "workbenchScene", "templateChecks", "scanAccountPreference", "focusOnlyActionable", "lastQianchuanManualSync"]);
  if (stored.preferredRole) currentRole = stored.preferredRole;
  if (SCENE_WORKBENCH[stored.workbenchScene]) workbenchScene = stored.workbenchScene;
  focusOnlyActionable = stored.focusOnlyActionable !== false;
  selectedQianchuanAccount = String(stored.scanAccountPreference || "");
  if (stored.templateChecks && typeof stored.templateChecks === "object") {
    const today = `${localDateKey()}:`;
    templateChecks = Object.fromEntries(Object.entries(stored.templateChecks).filter(([key]) => key.startsWith(today)));
    await chrome.storage.local.set({ templateChecks });
  }
  document.querySelectorAll("#role-nav button").forEach((item) => item.classList.toggle("active", item.dataset.role === currentRole));
  restoreQianchuanSyncUi(stored.lastQianchuanManualSync || {});
  renderWorkbench();
  applyModuleVisibility();
  refreshAll(false);
});
document.getElementById("refresh-button").addEventListener("click", () => refreshAll(false));
document.getElementById("sync-diagnose").addEventListener("click", () => refreshAll(true));
document.getElementById("refresh-oceanengine-status").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const result = document.getElementById("oceanengine-oauth-result");
  button.disabled = true;
  button.textContent = "正在刷新…";
  try {
    await refreshOceanEngineStatus();
  } catch (error) {
    result.textContent = `刷新失败：${error.message}`;
    result.className = "error";
  } finally {
    button.disabled = false;
    button.textContent = "刷新授权状态";
  }
});
document.getElementById("authorize-oceanengine").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const result = document.getElementById("oceanengine-oauth-result");
  const appId = document.getElementById("oceanengine-app-id").value.trim();
  const appSecret = document.getElementById("oceanengine-app-secret").value.trim();
  button.disabled = true;
  button.textContent = "正在打开授权页…";
  result.textContent = "正在生成本次安全授权链接。";
  result.className = "";
  try {
    const response = await bridgeFetch("/oauth/oceanengine/start", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
      body: JSON.stringify({ app_id: appId, app_secret: appSecret }),
    });
    if (!response.authorize_url) throw new Error("本地 Agent 未生成授权链接");
    await chrome.tabs.create({ url: response.authorize_url, active: true });
    document.getElementById("oceanengine-app-secret").value = "";
    result.textContent = "授权页面已打开：请选择要授权的千川账号并确认，完成后会自动回到本机 Agent。";
    startOceanEngineStatusPolling();
    await refreshOceanEngineStatus();
  } catch (error) {
    result.textContent = `无法开始授权：${error.message}`;
    result.className = "error";
  } finally {
    button.disabled = false;
    button.textContent = "授权千川账号";
  }
});
document.getElementById("sync-oceanengine-data").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const result = document.getElementById("oceanengine-sync-result");
  button.disabled = true;
  button.textContent = "正在读取官方数据…";
  result.textContent = "正在解析店铺与广告账户关系，并读取计划、报表和视频素材。";
  result.className = "";
  try {
    const response = await bridgeFetch("/oauth/oceanengine/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
      body: JSON.stringify({ days: 7 }),
    });
    renderOceanEngineSync(response);
    await loadDashboard();
  } catch (error) {
    result.textContent = `官方数据同步失败：${error.message}。已有浏览器快照不会丢失。`;
    result.className = "error";
  } finally {
    button.disabled = false;
    button.textContent = "同步官方数据";
  }
});
document.getElementById("focus-toggle").addEventListener("click", async () => {
  focusOnlyActionable = !focusOnlyActionable;
  await chrome.storage.local.set({ focusOnlyActionable });
  applyModuleVisibility();
});
document.getElementById("context-action").addEventListener("click", () => {
  if (!currentOperationContext?.selected_store?.key) {
    const select = document.getElementById("qianchuan-account-select");
    select.focus();
    select.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  const receipt = document.getElementById("scan-receipt-card");
  receipt.open = true;
  document.querySelector(".scan-card")?.scrollIntoView({ behavior: "smooth", block: "start" });
});
document.getElementById("manager-expand").addEventListener("click", () => {
  managerQueueExpanded = !managerQueueExpanded;
  if (!currentOperationsContext) return;
  const { ops, shelf, live, creative, coverage } = currentOperationsContext;
  renderOperations(ops, shelf, live, creative, coverage);
});
document.getElementById("preflight-reread").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在读取并复核…";
  try {
    const response = await chrome.runtime.sendMessage({ type: "sync-current-qianchuan" });
    if (!response?.ok) throw new Error(response?.error || "读取失败");
    await new Promise((resolve) => setTimeout(resolve, 500));
    await refreshExecutionPreflight();
  } catch (error) {
    document.getElementById("preflight-next").textContent = error.message || "读取失败，请先打开对应千川计划页面。";
  } finally {
    button.disabled = false;
    button.textContent = "读取当前千川页并复核";
  }
});
document.getElementById("preflight-stop").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  if (!currentPreflightSession?.session_id) return;
  button.disabled = true;
  button.textContent = "正在停止…";
  try {
    const response = await bridgeFetch("/actions/preflight/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
      body: JSON.stringify({ session_id: currentPreflightSession.session_id }),
    });
    renderExecutionPreflight(response.preflight || {});
  } catch (error) {
    document.getElementById("preflight-next").textContent = error.message || "停止失败";
    button.disabled = false;
  } finally {
    button.textContent = "紧急停止";
  }
});

document.getElementById("preflight-authorize").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  if (!currentPreflightSession?.session_id) return;
  const verb = button.dataset.operationType === "restore_budget" ? "恢复预算" : "降低预算";
  const confirmationText = `确认${verb}至${button.dataset.targetValue || ""}`;
  const entered = window.prompt(`这是最后一次人工授权，不会立即修改千川。\n请输入：${confirmationText}`, "");
  if (entered === null) return;
  button.disabled = true;
  try {
    const response = await bridgeFetch("/actions/preflight/authorize", {
      method: "POST",
      body: JSON.stringify({
        session_id: currentPreflightSession.session_id,
        confirmation_text: entered,
      }),
    });
    renderExecutionPreflight(response.preflight || {});
    const authorizationId = response.preflight?.session?.authorization_id;
    if (authorizationId) {
      const execution = await chrome.runtime.sendMessage({
        type: "run-authorized-execution",
        authorization_id: authorizationId,
      });
      if (!execution?.ok) throw new Error(execution?.error || "预算受监督执行失败");
      document.getElementById("preflight-next").textContent = execution.result?.verification?.verified
        ? "千川预算已提交，并通过执行后页面回读验收。"
        : "平台已返回提交成功，但执行后页面回读尚未匹配；请勿重复执行，先重新同步当前千川页。";
    }
  } catch (error) {
    document.getElementById("preflight-next").textContent = error.message || "最终授权失败";
  } finally {
    button.disabled = false;
  }
});
document.getElementById("full-scan-button").addEventListener("click", async () => {
  if (!selectedQianchuanAccount) {
    const select = document.getElementById("qianchuan-account-select");
    select.focus();
    document.getElementById("scan-detail").textContent = "请先选择当前店铺，避免把多个千川账号的数据混在一起。";
    return;
  }
  await chrome.runtime.sendMessage({ type: "start-full-scan", account_key: selectedQianchuanAccount });
  await loadDashboard();
});
document.getElementById("qianchuan-account-select").addEventListener("change", async (event) => {
  selectedQianchuanAccount = event.currentTarget.value;
  accountSelectionRequired = false;
  await chrome.storage.local.set({ scanAccountPreference: selectedQianchuanAccount });
  await bridgeFetch("/settings", { method: "POST", headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" }, body: JSON.stringify({ qianchuan_account_key: selectedQianchuanAccount }) });
  await loadDashboard();
});
document.getElementById("current-qianchuan-button").addEventListener("click", () => {
  syncRecentQianchuanPage().catch(() => undefined);
});
document.getElementById("qianchuan-sync-dock-button").addEventListener("click", () => {
  syncRecentQianchuanPage().catch(() => undefined);
});
document.getElementById("cancel-scan-button").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "cancel-full-scan" });
  document.getElementById("scan-detail").textContent = "正在安全停止…";
});
document.getElementById("retry-scan-button").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "retry-failed-scan" });
  await loadDashboard();
});
document.getElementById("role-nav").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-role]");
  if (!button) return;
  currentRole = button.dataset.role;
  managerQueueExpanded = false;
  document.querySelectorAll("#role-nav button").forEach((item) => item.classList.toggle("active", item === button));
  chrome.storage.local.set({ preferredRole: currentRole });
  renderWorkbench();
  if (currentOperationsContext) {
    const { ops, shelf, live, creative, coverage } = currentOperationsContext;
    renderOperations(ops, shelf, live, creative, coverage);
  } else applyModuleVisibility();
});
document.getElementById("workbench-scene").addEventListener("change", async (event) => {
  workbenchScene = SCENE_WORKBENCH[event.currentTarget.value] ? event.currentTarget.value : "daily";
  await chrome.storage.local.set({ workbenchScene });
  renderWorkbench();
});
document.getElementById("copy-brief").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  if (!latestBrief) {
    button.textContent = "暂无简报内容";
    setTimeout(() => { button.textContent = "复制简报"; }, 1500);
    return;
  }
  try {
    await navigator.clipboard.writeText(latestBrief);
    button.textContent = "已复制";
  } catch {
    button.textContent = "复制失败";
  }
  setTimeout(() => { button.textContent = "复制简报"; }, 1200);
});
document.getElementById("export-tasks").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在生成…";
  try {
    const result = await bridgeFetch("/tasks/export?format=clipboard");
    if (result.content) {
      await navigator.clipboard.writeText(result.content);
      button.textContent = "已复制任务清单";
    } else {
      button.textContent = "无任务可导出";
    }
  } catch (error) {
    button.textContent = "导出失败";
  } finally {
    setTimeout(() => { button.disabled = false; button.textContent = "导出任务"; }, 1500);
  }
});
document.getElementById("save-settings").addEventListener("click", async () => {
  const status = document.getElementById("settings-status");
  try {
    const payload = {
      execution_mode: document.getElementById("execution-mode").value,
      roi_target: Number(document.getElementById("roi-target").value),
      min_spend_for_action: Number(document.getElementById("spend-threshold").value),
      low_inventory_threshold: Number(document.getElementById("stock-threshold").value),
      max_daily_execution_count: Number(document.getElementById("daily-execution-limit").value),
      max_daily_budget_reduction: Number(document.getElementById("daily-budget-limit").value),
      execution_cooldown_minutes: Number(document.getElementById("execution-cooldown").value),
    };
    await bridgeFetch("/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
      body: JSON.stringify(payload),
    });
    const pages = Math.max(1, Math.min(20, Number(document.getElementById("max-deep-scan-pages").value) || 5));
    const extensionSettings = await chrome.runtime.sendMessage({
      type: "update-settings",
      settings: { maxDeepScanPages: pages },
    });
    if (!extensionSettings?.ok) throw new Error(extensionSettings?.error || "扩展设置保存失败");
    status.textContent = "设置已保存；深度巡查页数写入当前浏览器，截断仍禁止确认资金动作。";
    await loadDashboard();
  } catch (error) {
    status.textContent = `保存失败：${error.message}`;
  }
});

document.getElementById("report-template").addEventListener("change", (event) => {
  const template = REPORT_TEMPLATE_LABELS[event.currentTarget.value] ? event.currentTarget.value : "default";
  document.getElementById("custom-template-wrap").hidden = template !== "custom";
  document.getElementById("report-template-label").textContent = REPORT_TEMPLATE_LABELS[template];
});

document.getElementById("save-report-settings").addEventListener("click", async () => {
  const status = document.getElementById("report-status");
  try {
    const payload = {
      daily_report_time: document.getElementById("report-time").value,
      daily_report_enabled: document.getElementById("report-enabled").checked,
      report_template: document.getElementById("report-template").value,
      custom_report_template: document.getElementById("custom-report-template").value,
    };
    await bridgeFetch("/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
      body: JSON.stringify(payload),
    });
    status.textContent = `日志设置已保存：${REPORT_TEMPLATE_LABELS[payload.report_template] || "默认模板"}`;
    await loadDashboard();
  } catch (error) {
    status.textContent = `保存失败：${error.message}`;
  }
});

async function generateReport(notify, button) {
  const status = document.getElementById("report-status");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = notify ? "正在生成并发送…" : "正在生成…";
  try {
    const result = await bridgeFetch("/reports/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
      body: JSON.stringify({ notify }),
    });
    const deliveries = result.deliveries || [];
    if (!notify) {
      status.textContent = `日志已生成：${result.report.date} · ${REPORT_TEMPLATE_LABELS[result.report.template] || "默认模板"}`;
    } else if (!deliveries.length) {
      status.textContent = "日志已生成，但尚未连接飞书或钉钉。";
    } else {
      const success = deliveries.filter((item) => item.ok).length;
      const failed = deliveries.length - success;
      status.textContent = `日志已生成并发送：成功 ${success}，失败 ${failed}`;
    }
  } catch (error) {
    status.textContent = `生成失败：${error.message}`;
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

document.getElementById("generate-report").addEventListener("click", (event) => generateReport(false, event.currentTarget));
document.getElementById("generate-send-report").addEventListener("click", (event) => generateReport(true, event.currentTarget));

async function saveIntegrationPatch(patch) {
  return bridgeFetch("/integrations/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
    body: JSON.stringify(patch),
  });
}

document.querySelectorAll("[data-integration-test]").forEach((button) => {
  button.addEventListener("click", async () => {
    const platform = button.dataset.integrationTest;
    const input = document.getElementById(`${platform}-webhook`);
    const result = document.getElementById(`${platform}-result`);
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "正在测试…";
    result.className = "";
    try {
      if (input.value.trim()) {
        await saveIntegrationPatch({ [`${platform}_webhook`]: input.value.trim() });
      }
      await bridgeFetch("/integrations/test", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
        body: JSON.stringify({ platform }),
      });
      result.textContent = "连接成功，测试消息已发送到群。";
      result.className = "ok";
      await loadDashboard();
    } catch (error) {
      result.textContent = `连接失败：${error.message}`;
      result.className = "error";
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  });
});

document.querySelectorAll("[data-integration-clear]").forEach((button) => {
  button.addEventListener("click", async () => {
    const platform = button.dataset.integrationClear;
    const result = document.getElementById(`${platform}-result`);
    try {
      await saveIntegrationPatch({ [`${platform}_webhook`]: "" });
      result.textContent = "连接已清除。";
      result.className = "ok";
      await loadDashboard();
    } catch (error) {
      result.textContent = `清除失败：${error.message}`;
      result.className = "error";
    }
  });
});

document.getElementById("save-integration-settings").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await saveIntegrationPatch({ auto_send_reports: document.getElementById("auto-send-reports").checked });
    button.textContent = "发送设置已保存";
    await loadDashboard();
  } catch (error) {
    button.textContent = `保存失败：${error.message}`;
  } finally {
    setTimeout(() => { button.disabled = false; button.textContent = "保存发送设置"; }, 1500);
  }
});
