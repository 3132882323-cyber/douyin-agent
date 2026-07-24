const BRIDGE_URL = "http://127.0.0.1:8765";
const LABELS = {
  doudian: "抖店", qianchuan: "千川", overview: "概览", orders: "订单",
  refunds: "售后", products: "商品", inventory: "库存", reviews: "评价",
  live: "直播", compass: "罗盘", funds: "资金", campaigns: "计划",
  report: "报表", materials: "素材", video_library: "视频库", live_dashboard: "直播大屏", account: "账户", shelf: "货架",
  qianchuan_live: "直播推广", qianchuan_campaigns: "商品推广", qianchuan_live_dashboard: "直播大屏", qianchuan_video_library: "视频库", unknown: "其他",
};

let latestBrief = "";
let currentRole = "运营总管";
let currentOps = null;
let scanPoller = null;
let scanStartTime = 0;
const SCAN_TIMEOUT_MS = 5 * 60 * 1000; // 5 minutes
let selectedQianchuanAccount = "";
let accountSelectionRequired = false;

async function pollFullScan() {
  const response = await chrome.runtime.sendMessage({ type: "get-dashboard" });
  if (response?.ok) renderFullScan(response.dashboard?.fullScan || {});
}

async function bridgeFetch(path, options = {}) {
  const response = await fetch(`${BRIDGE_URL}${path}`, { cache: "no-store", ...options });
  if (!response.ok) throw new Error(`本地 Agent 返回 HTTP ${response.status}`);
  return response.json();
}

function renderConnection(ok, title, detail) {
  const element = document.getElementById("connection");
  element.className = `connection ${ok ? "ok" : "error"}`;
  element.querySelector("strong").textContent = title;
  element.querySelector("p").textContent = detail;
}

function renderFullScan(scan = {}) {
  const running = scan.status === "running";
  // Track scan start time for timeout detection
  if (running && !scanStartTime) scanStartTime = Date.now();
  if (!running) scanStartTime = 0;
  // Auto-cancel if scan exceeds timeout
  if (running && scanStartTime && (Date.now() - scanStartTime > SCAN_TIMEOUT_MS)) {
    chrome.runtime.sendMessage({ type: "cancel-full-scan" }).catch(() => undefined);
    document.getElementById("scan-detail").textContent = `巡检超过 ${SCAN_TIMEOUT_MS / 60000} 分钟已自动停止，请检查页面状态后重试`;
    return;
  }
  const state = document.getElementById("scan-state");
  const labels = { idle: "未运行", running: "巡检中", completed: "已完成", partial: "部分完成", cancelled: "已停止", interrupted: "已中断", error: "失败" };
  state.textContent = labels[scan.status] || "未运行";
  state.className = `scan-tag ${running || scan.status === "completed" ? "ok" : ["partial", "interrupted"].includes(scan.status) ? "warn" : scan.status === "error" ? "error" : "idle"}`;
  const total = Number(scan.total || 18);
  const index = Number(scan.index || 0);
  document.getElementById("scan-progress-bar").style.width = `${Math.min(100, total ? index / total * 100 : 0)}%`;
  document.getElementById("scan-detail").textContent = running ? `正在采集：${scan.current || "准备中"}（${index}/${total}）` : scan.finished_at ? `上次完成：成功 ${scan.success || 0}，失败 ${scan.failed || 0}` : "按清单自动打开页面并采集，不需要 API";
  const rows = (scan.results || []).reduce((sum, item) => sum + Number(item.quality?.row_count || 0), 0);
  const virtualPasses = (scan.results || []).reduce((sum, item) => sum + Number(item.quality?.virtual_scroll_passes || 0), 0);
  document.getElementById("scan-summary").textContent = scan.error ? `失败原因：${scan.error}` : `成功 ${scan.success || 0} 页，失败 ${scan.failed || 0} 页，低质量 ${scan.low_quality || 0} 页；读取 ${rows} 行，滚动采集 ${virtualPasses} 次`;
  document.getElementById("full-scan-button").disabled = running || accountSelectionRequired;
  document.getElementById("full-scan-button").textContent = running ? "正在自动获取…" : accountSelectionRequired ? "请先选择千川账号" : "自动获取全店数据";
  document.getElementById("cancel-scan-button").hidden = !running;
  document.getElementById("retry-scan-button").hidden = running || !(scan.failed > 0);
  if (running && !scanPoller) scanPoller = setInterval(() => pollFullScan().catch(() => undefined), 1500);
  if (!running && scanPoller) { clearInterval(scanPoller); scanPoller = null; }
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

function recommendationCard(item, kind) {
  const card = document.createElement("article");
  card.className = `recommendation-card ${item.level || "info"}`;
  const top = document.createElement("div");
  top.className = "recommendation-top";
  const title = document.createElement("strong");
  title.textContent = kind === "plan" ? item.plan : item.product;
  const tag = document.createElement("span");
  tag.textContent = item.level === "high" ? "高优先" : item.level === "opportunity" ? "可放量" : "需关注";
  top.append(title, tag);
  const suggestion = document.createElement("p");
  suggestion.textContent = item.suggestion || "请回到后台核对。";
  const reason = document.createElement("small");
  reason.textContent = kind === "plan" ? item.reason || "" : item.title || "";
  card.append(top, suggestion, reason);
  return card;
}

function renderPlans(items = []) {
  const container = document.getElementById("plans");
  document.getElementById("plan-count").textContent = `${items.length} 项`;
  if (!items.length) return empty(container, "暂无计划级建议，请同步千川计划和报表页面");
  container.className = "stack";
  container.replaceChildren(...items.slice(0, 8).map(planWorkbenchCard));
}

function planWorkbenchCard(item) {
  const card = document.createElement("article");
  card.className = `plan-workbench-card ${item.level || "info"}`;
  const top = document.createElement("div");
  top.className = "recommendation-top";
  const title = document.createElement("strong"); title.textContent = item.plan || "千川计划";
  const tag = document.createElement("span");
  tag.textContent = item.level === "high" ? "立即处理" : item.level === "opportunity" ? "具备放量条件" : "今日处理";
  top.append(title, tag);
  const diagnosis = document.createElement("h4"); diagnosis.textContent = item.diagnosis || "计划需要复核";
  const steps = document.createElement("div"); steps.className = "plan-steps";
  [
    ["发现了什么", item.found || item.reason],
    ["为什么判断", item.judgment],
    ["建议动作", item.action || item.suggestion],
    ["建议调整范围", item.adjustment_range],
    ["观察多久", item.observation_window],
    ["用什么指标验收", item.acceptance],
  ].forEach(([label, value]) => {
    const row = document.createElement("div");
    const key = document.createElement("b"); key.textContent = label;
    const text = document.createElement("p"); text.textContent = value || "--";
    row.append(key, text); steps.append(row);
  });
  const guardrail = document.createElement("small");
  guardrail.className = "plan-guardrail";
  guardrail.textContent = item.guardrail || "所有预算、出价和启停操作均需投手人工确认。";
  const actions = document.createElement("div"); actions.className = "plan-task-actions";
  const state = document.createElement("span");
  const labels = { todo: "待处理", doing: "进行中", observing: "待观察", done: "已完成" };
  state.textContent = item.task_updated_at ? `已加入 · ${labels[item.task_status] || "待处理"}` : "尚未加入今日任务";
  const button = document.createElement("button");
  const next = item.task_updated_at
    ? item.task_status === "todo" ? ["开始处理", "doing"]
      : item.task_status === "doing" ? ["转待观察", "observing"]
        : item.task_status === "observing" ? ["标记完成", "done"] : ["重新打开", "todo"]
    : ["添加到任务", "todo"];
  [button.textContent] = next;
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await bridgeFetch("/tasks/update", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
        body: JSON.stringify({ task_id: item.task_id, status: next[1] }),
      });
      await loadDashboard();
    } catch (error) {
      state.textContent = `操作失败：${error.message}`;
      button.disabled = false;
    }
  });
  actions.append(state, button);
  card.append(top, diagnosis, steps, guardrail, actions);
  return card;
}

function renderInventory(items = []) {
  const container = document.getElementById("inventory");
  document.getElementById("inventory-count").textContent = `${items.length} 项`;
  if (!items.length) return empty(container, "暂无库存预警，或尚未同步商品/库存页面");
  container.className = "stack";
  container.replaceChildren(...items.slice(0, 8).map((item) => recommendationCard(item, "inventory")));
}

function renderCreativeAnalysis(creative = {}) {
  const summary = creative.summary || {};
  document.getElementById("creative-status").textContent = creative.data_status === "ready" ? "数据已就绪" : "待同步";
  document.getElementById("creative-count").textContent = `${summary.total_videos || 0} 条`;
  renderMetricStrip("creative-metrics", {
    视频数: summary.total_videos || 0,
    有消耗: summary.spending_videos || 0,
    未测试: summary.untested_videos || 0,
    高风险: summary.risky_videos || 0,
    高潜: summary.high_potential_videos || 0,
  });
  renderTasks("creative-actions", creative.recommendations || []);
  const container = document.getElementById("creative-videos");
  const videos = creative.videos || [];
  if (!videos.length) return empty(container, "暂无视频数据，请同步巨量千川视频库");
  container.className = "stack";
  container.replaceChildren(...videos.slice(0, 8).map((item) => recommendationCard({
    plan: item.name,
    level: item.level,
    suggestion: item.suggestion,
    reason: `${item.status} · 消耗 ${item.evidence?.spend == null ? "--" : item.evidence.spend} · ROI ${item.evidence?.roi == null ? "--" : item.evidence.roi}`,
  }, "plan")));
}

function taskCard(item) {
  const card = document.createElement("article");
  card.className = `task-card ${item.level || "info"}`;
  const meta = document.createElement("div");
  meta.className = "task-meta";
  meta.textContent = `${item.owner || "运营"} · ${item.level === "high" ? "立即处理" : item.level === "opportunity" ? "增长机会" : "今日处理"}`;
  const title = document.createElement("strong"); title.textContent = item.title || "运营任务";
  const action = document.createElement("p"); action.textContent = item.action || item.suggestion || "请核对后台。";
  const chips = document.createElement("div"); chips.className = "task-chips";
  [item.impact, item.confidence === "high" ? "高可信" : "需观察"].filter(Boolean).forEach((value) => {
    const chip = document.createElement("span"); chip.textContent = value; chips.append(chip);
  });
  const detail = document.createElement("details"); detail.className = "task-detail";
  const detailSummary = document.createElement("summary"); detailSummary.textContent = "查看依据与完成标准";
  const evidence = document.createElement("small"); evidence.textContent = `依据：${item.evidence || "当前页面数据"}`;
  const acceptance = document.createElement("small"); acceptance.textContent = `完成标准：${item.acceptance || "人工核对完成"}`;
  detail.append(detailSummary, evidence, acceptance);
  card.append(meta, title, action, chips, detail);
  if (item.id) {
    const actions = document.createElement("div"); actions.className = "task-actions";
    const statusLabel = document.createElement("span");
    const labels = { todo: "待处理", doing: "进行中", observing: "待观察", done: "已完成" };
    statusLabel.textContent = labels[item.status] || "待处理";
    const transitions = item.status === "todo" ? [["开始处理", "doing"]]
      : item.status === "doing" ? [["转待观察", "observing"], ["完成", "done"]]
      : item.status === "observing" ? [["完成", "done"]] : [["重新打开", "todo"]];
    actions.append(statusLabel);
    transitions.forEach(([label, status]) => {
      const button = document.createElement("button"); button.textContent = label; button.setAttribute("aria-label", `${label}：${item.title || '任务'}`);
      button.addEventListener("click", async () => {
        button.disabled = true;
        // When starting a task, save a suggestion snapshot for effectiveness tracking
        if (status === "doing" && item.status === "todo") {
          bridgeFetch("/tasks/track", { method: "POST", headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" }, body: JSON.stringify({ task_id: item.id, context: { title: item.title, owner: item.owner } }) }).catch(() => undefined);
        }
        await bridgeFetch("/tasks/update", { method: "POST", headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" }, body: JSON.stringify({ task_id: item.id, status }) });
        await loadDashboard();
      });
      actions.append(button);
    });
    card.append(actions);
    // Feedback buttons
    const feedback = document.createElement("div"); feedback.className = "task-feedback";
    const fbLabel = document.createElement("small"); fbLabel.textContent = "这条建议有用吗？";
    const fbUp = document.createElement("button"); fbUp.className = "fb-btn"; fbUp.textContent = "\u{1F44D} \u6709\u7528"; fbUp.setAttribute("aria-label", `对"${item.title || '建议'}"点赞`);
    const fbDown = document.createElement("button"); fbDown.className = "fb-btn"; fbDown.textContent = "\u{1F44E} \u6CA1\u7528"; fbDown.setAttribute("aria-label", `对"${item.title || '建议'}"点踩`);
    const fbStatus = document.createElement("small"); fbStatus.className = "fb-status";
    [fbUp, fbDown].forEach((btn, index) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await bridgeFetch("/feedback", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
            body: JSON.stringify({ task_id: item.id, rating: index === 0 ? "up" : "down", context: item.title || "" }),
          });
          fbStatus.textContent = "感谢反馈";
          fbUp.disabled = true; fbDown.disabled = true;
        } catch { fbStatus.textContent = "反馈失败"; btn.disabled = false; }
      });
    });
    feedback.append(fbLabel, fbUp, fbDown, fbStatus);
    card.append(feedback);
  }
  return card;
}

function renderTasks(id, items = []) {
  const container = document.getElementById(id);
  if (!items.length) return empty(container, "暂无专项任务，或尚未同步对应页面");
  container.className = "stack";
  container.replaceChildren(...items.slice(0, 8).map(taskCard));
}

function renderMetricStrip(id, metrics) {
  const container = document.getElementById(id);
  const entries = Object.entries(metrics).filter(([, value]) => value !== null && value !== undefined).slice(0, 5);
  container.replaceChildren(...entries.map(([label, value]) => {
    const cell = document.createElement("div");
    const strong = document.createElement("strong"); strong.textContent = typeof value === "number" ? Number(value.toFixed(1)).toLocaleString() : value;
    const small = document.createElement("small"); small.textContent = label;
    cell.append(strong, small); return cell;
  }));
}

function roleTasks(ops, opportunity = false) {
  const source = ops.all_tasks || [];
  return source.filter((item) => item.status !== "done" && (currentRole === "运营总管" || item.owner === currentRole) && (opportunity ? item.level === "opportunity" : item.level !== "opportunity"));
}

function renderOperations(ops, shelf, live, creative, coverage = []) {
  currentOps = ops;
  const tasks = roleTasks(ops, false).slice(0, 3);
  const growth = roleTasks(ops, true).slice(0, 3);
  document.getElementById("task-heading").textContent = currentRole === "运营总管" ? "今日必须处理" : `${currentRole} · 今日必做`;
  document.getElementById("manager-count").textContent = `${tasks.length} 项`;
  renderTasks("manager-tasks", tasks);
  document.getElementById("growth-count").textContent = `${growth.length} 项`;
  renderTasks("growth-tasks", growth);
  const scoped = (ops.all_tasks || []).filter((item) => currentRole === "运营总管" || item.owner === currentRole);
  const done = scoped.filter((item) => item.status === "done").length;
  document.getElementById("progress-rate").textContent = scoped.length ? `${Math.round(done / scoped.length * 100)}%` : "--";
  document.getElementById("doing-count").textContent = scoped.filter((item) => item.status === "doing").length;
  document.getElementById("observing-count").textContent = scoped.filter((item) => item.status === "observing").length;
  const fresh = coverage.filter((item) => item.fresh).length;
  document.getElementById("data-freshness").textContent = coverage.length ? `${fresh}/${coverage.length}` : "--";
  document.querySelectorAll(".module-section").forEach((section) => { section.hidden = currentRole !== "运营总管" && !String(section.dataset.owner || "").split(/\s+/).includes(currentRole); });
  document.getElementById("shelf-status").textContent = shelf.data_status === "ready" ? "数据已就绪" : "待同步";
  renderMetricStrip("shelf-metrics", { 曝光: shelf.funnel?.exposure, 点击: shelf.funnel?.clicks, 成交人数: shelf.funnel?.buyers, 点击率: shelf.funnel?.click_rate == null ? null : `${shelf.funnel.click_rate.toFixed(1)}%` });
  renderTasks("shelf-actions", shelf.recommendations || []);
  document.getElementById("live-status").textContent = live.data_status === "ready" ? "数据已就绪" : "待同步";
  renderMetricStrip("live-metrics", { 进房: live.funnel?.views, 进房率: live.funnel?.enter_rate == null ? null : `${live.funnel.enter_rate.toFixed(1)}%`, 商品点击: live.funnel?.product_clicks, 订单: live.funnel?.orders, ROI: live.funnel?.roi });
  renderTasks("live-actions", live.recommendations || []);
  renderCreativeAnalysis(creative || {});
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

function renderSettings(settings) {
  document.getElementById("roi-target").value = settings.roi_target;
  document.getElementById("spend-threshold").value = settings.min_spend_for_action;
  document.getElementById("stock-threshold").value = settings.low_inventory_threshold;
  document.getElementById("report-time").value = settings.daily_report_time;
  document.getElementById("report-enabled").checked = settings.daily_report_enabled;
}

function renderQianchuanAccounts(payload = {}) {
  const select = document.getElementById("qianchuan-account-select");
  const accounts = payload.accounts || [];
  selectedQianchuanAccount = String(payload.selected_account_key || "");
  select.replaceChildren();
  const current = document.createElement("option");
  current.value = "";
  current.textContent = "当前千川页面（不校验账号）";
  select.append(current);
  accounts.forEach((account) => {
    const option = document.createElement("option");
    option.value = account.key;
    option.textContent = account.label;
    select.append(option);
  });
  if (selectedQianchuanAccount && accounts.some((account) => account.key === selectedQianchuanAccount)) {
    select.value = selectedQianchuanAccount;
  }
  accountSelectionRequired = accounts.length > 1 && !selectedQianchuanAccount;
  document.getElementById("qianchuan-account-hint").textContent = selectedQianchuanAccount
    ? "巡查只分析所选账号；如后台账号不一致会停止千川采集。"
    : accountSelectionRequired
      ? "当前页面可直接读取；如需全店巡查，请先选择一个千川账号。"
      : "当前页面模式不会校验账号；适合账号识别失败时直接读取。";
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

async function loadDashboard() {
  // Show loading skeleton
  showLoadingSkeleton();
  // Remember focus before re-render
  const focusedEl = document.activeElement;
  const focusId = focusedEl?.id || focusedEl?.closest("[id]")?.id;

  const [
    insightsR, actionCenterR, settingsR, opsR, extensionR, trendsR, accountsR, healthR, effectivenessR
  ] = await Promise.allSettled([
    bridgeFetch("/insights"),
    bridgeFetch("/action-center"),
    bridgeFetch("/settings"),
    bridgeFetch("/ops-manager"),
    chrome.runtime.sendMessage({ type: "get-dashboard" }),
    bridgeFetch("/trends?days=7"),
    bridgeFetch("/qianchuan-accounts"),
    bridgeFetch("/health-monitor"),
    bridgeFetch("/effectiveness"),
  ]);

  const val = (r, fallback) => r.status === "fulfilled" ? r.value : fallback;
  const insights = val(insightsR, { coverage: [], alerts: [], headline: "数据加载异常", summary: "部分模块连接失败，请稍后重试" });
  const actionCenter = val(actionCenterR, { plan_recommendations: [], inventory_alerts: [], shelf_analysis: {}, live_analysis: {}, creative_analysis: {} });
  const settings = val(settingsR, { roi_target: 1.5, min_spend_for_action: 100, low_inventory_threshold: 10, daily_report_time: "09:00", daily_report_enabled: true });
  const ops = val(opsR, { all_tasks: [], today_top_actions: [] });
  const extensionResponse = val(extensionR, {});
  const trends = val(trendsR, {});
  const accounts = val(accountsR, { accounts: [], selected_account_key: "" });
  const health = val(healthR, {});
  const effectiveness = val(effectivenessR, {});

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
  renderPlans(actionCenter.plan_recommendations || []);
  renderInventory(actionCenter.inventory_alerts || []);
  renderOperations(ops, actionCenter.shelf_analysis || {}, actionCenter.live_analysis || {}, actionCenter.creative_analysis || {}, insights.coverage || []);
  renderAlerts(insights.alerts || []);
  renderCoverage(insights.coverage || []);
  renderSettings(settings);
  renderQianchuanAccounts(accounts);
  renderFullScan(extensionResponse?.dashboard?.fullScan || {});
  renderTrends(trends);
  renderHealthMonitor(health);
  renderEffectiveness(effectiveness);

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

document.addEventListener("DOMContentLoaded", async () => {
  const stored = await chrome.storage.local.get("preferredRole");
  if (stored.preferredRole) currentRole = stored.preferredRole;
  document.querySelectorAll("#role-nav button").forEach((item) => item.classList.toggle("active", item.dataset.role === currentRole));
  refreshAll(false);
});
document.getElementById("refresh-button").addEventListener("click", () => refreshAll(false));
document.getElementById("sync-diagnose").addEventListener("click", () => refreshAll(true));
document.getElementById("full-scan-button").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "start-full-scan", account_key: selectedQianchuanAccount });
  await loadDashboard();
});
document.getElementById("qianchuan-account-select").addEventListener("change", async (event) => {
  selectedQianchuanAccount = event.currentTarget.value;
  accountSelectionRequired = false;
  await bridgeFetch("/settings", { method: "POST", headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" }, body: JSON.stringify({ qianchuan_account_key: selectedQianchuanAccount }) });
  await loadDashboard();
});
document.getElementById("current-qianchuan-button").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const hint = document.getElementById("qianchuan-account-hint");
  button.disabled = true;
  button.textContent = "正在读取当前页面…";
  try {
    selectedQianchuanAccount = "";
    document.getElementById("qianchuan-account-select").value = "";
    await bridgeFetch("/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
      body: JSON.stringify({ qianchuan_account_key: "" }),
    });
    const response = await chrome.runtime.sendMessage({ type: "sync-current-qianchuan" });
    if (!response?.ok) throw new Error(response?.error || "读取失败");
    const accountLabel = response.result?.account?.label ? ` · ${response.result.account.label}` : "";
    hint.textContent = `读取成功：${LABELS[response.result?.page_type] || response.result?.page_type || "千川页面"}${accountLabel}`;
    await loadDashboard();
  } catch (error) {
    hint.textContent = error.message || "读取失败，请先切换到巨量千川页面";
  } finally {
    button.disabled = false;
    button.textContent = "读取当前千川页面";
  }
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
  document.querySelectorAll("#role-nav button").forEach((item) => item.classList.toggle("active", item === button));
  chrome.storage.local.set({ preferredRole: currentRole });
  if (currentOps) loadDashboard();
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
      roi_target: Number(document.getElementById("roi-target").value),
      min_spend_for_action: Number(document.getElementById("spend-threshold").value),
      low_inventory_threshold: Number(document.getElementById("stock-threshold").value),
      daily_report_time: document.getElementById("report-time").value,
      daily_report_enabled: document.getElementById("report-enabled").checked,
    };
    await bridgeFetch("/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
      body: JSON.stringify(payload),
    });
    status.textContent = "设置已保存，建议已按新阈值刷新。";
    await loadDashboard();
  } catch (error) {
    status.textContent = `保存失败：${error.message}`;
  }
});
document.getElementById("generate-report").addEventListener("click", async () => {
  const status = document.getElementById("settings-status");
  try {
    const result = await bridgeFetch("/reports/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
      body: "{}",
    });
    status.textContent = `日报已生成：${result.report.date}`;
  } catch (error) {
    status.textContent = `生成失败：${error.message}`;
  }
});
