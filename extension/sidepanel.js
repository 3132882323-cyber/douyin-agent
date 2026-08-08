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
let currentRole = "货架商品";
let currentOps = null;
let currentOperationsContext = null;
let scanPoller = null;
let scanStartTime = 0;
let workbenchScene = "daily";
let templateChecks = {};
let managerQueueExpanded = false;
let currentPreflightSession = null;
let qianchuanSyncPromise = null;
let oceanengineStatusPoller = null;
let currentOperationContext = null;
let currentOnboarding = null;
let currentConnectionGuide = null;
let qianchuanFeatureDeferred = false;
let currentPromotionView = "chengfang";
const SCAN_TIMEOUT_MS = 5 * 60 * 1000; // 5 minutes
const DOUDIAN_SCAN_PAGE_IDS = ["overview", "orders", "refunds", "products", "inventory", "reviews", "shelf", "live", "short_video", "image_text", "recommend_card"];

const ROLE_WORKBENCH = {
  "货架商品": {
    title: "货架商品工作台",
    description: "把商品承接、库存和货架转化放在同一条经营链路中",
    tasks: [
      ["shelf_funnel", "核对货架漏斗", "定位曝光、点击或成交环节的最大损失"],
      ["stock_risk", "检查主推商品库存", "缺货风险、可售天数和流量安排已经对齐"],
      ["shelf_assets", "检查商品承接", "主图、标题、搜索卡和成交表现已建立优化任务"],
    ],
  },
  "直播投放": {
    title: "直播投放工作台",
    description: "把直播漏斗、千川消耗和受控执行放在同一个决策面板",
    tasks: [
      ["live_funnel", "核对直播与投放漏斗", "进房、商品点击、成交、消耗和 ROI 瓶颈已定位"],
      ["ad_risk", "处理高消耗低转化计划", "每项调整都有依据、幅度、授权和观察窗口"],
      ["live_review", "复盘直播投放结果", "异常时段已关联到计划调整和下一次复查任务"],
    ],
  },
  "内容": {
    title: "内容工作台",
    description: "独立管理素材、视频、脚本和创意测试，不与投放操作混在一起",
    tasks: [
      ["content_performance", "检查内容表现", "停测、复用和补测素材已经分组"],
      ["content_pipeline", "补齐内容测试池", "脚本、钩子、卖点和视频版本都有明确测试任务"],
      ["content_review", "沉淀有效内容", "有效素材已记录适用人群、场景和复用方式"],
    ],
  },
};

const ROLE_MIGRATION = {
  运营总管: "货架商品",
  货架运营: "货架商品",
  商品运营: "货架商品",
  直播运营: "直播投放",
  投放运营: "直播投放",
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
  const role = ROLE_WORKBENCH[currentRole] || ROLE_WORKBENCH["货架商品"];
  const scene = SCENE_WORKBENCH[workbenchScene] || SCENE_WORKBENCH.daily;
  return [scene.task, ...role.tasks.slice(0, 2)].map(([id, title, acceptance]) => ({ id, title, acceptance }));
}

function templateCheckKey(taskId) {
  return `${selectedStoreKey || "unscoped"}:${localDateKey()}:${workbenchScene}:${currentRole}:${taskId}`;
}

function renderWorkbench() {
  const role = ROLE_WORKBENCH[currentRole] || ROLE_WORKBENCH["货架商品"];
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
let selectedStoreKey = "";
let accountSelectionRequired = false;

async function pollFullScan() {
  const response = await chrome.runtime.sendMessage({ type: "get-dashboard" });
  if (response?.ok) renderFullScan(response.dashboard?.fullScan || {});
}

async function bridgeFetch(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.method && options.method !== "GET") headers["X-Dian-Agent"] ||= "2";
  if (options.body) headers["Content-Type"] ||= "application/json";
  const response = await fetch(`${BRIDGE_URL}${path}`, { cache: "no-store", ...options, headers });
  const value = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(value.error || `本地 Agent 返回 HTTP ${response.status}`);
  return value;
}

function renderConnection(ok, title, detail) {
  const element = document.getElementById("connection");
  element.className = `connection ${ok ? "ok" : "error"}`;
  element.querySelector("strong").textContent = title;
  element.querySelector("p").textContent = detail;
}

function scanReceiptFromStatus(scan = {}) {
  const results = (scan.results || []).filter((item) => item && typeof item === "object").map((item) => {
    const score = Math.max(0, Math.min(100, Number(item.quality?.score || 0)));
    return {
      ...item,
      source: item.source || (String(item.id || "").startsWith("qianchuan") ? "qianchuan" : "doudian"),
      quality_score: score,
      metric_count: Number(item.quality?.metric_count || 0),
      row_count: Number(item.quality?.row_count || 0),
      needs_review: Boolean(item.ok) && score < 70,
    };
  });
  const total = Math.max(Number(scan.total || 0), results.length);
  const success = results.filter((item) => item.ok).length;
  const failed = results.filter((item) => !item.ok).length;
  const needsReview = results.filter((item) => item.needs_review).length;
  const coverageRate = total ? Math.round(results.length / total * 100) : 0;
  const running = scan.status === "running";
  const ready = scan.status === "completed" && coverageRate === 100 && failed === 0 && needsReview === 0;
  return {
    scan_status: scan.status || "idle",
    readiness: running ? "running" : ready ? "ready" : results.length ? "attention" : "empty",
    readiness_label: running ? "正在采集" : ready ? "数据可用于分析" : results.length ? "需要补采或复核" : "等待巡查",
    account_label: results.find((item) => item.account_label)?.account_label || "",
    finished_at: Number(scan.finished_at || 0),
    summary: {
      total,
      completed: results.length,
      success,
      failed,
      needs_review: needsReview,
      coverage_rate: coverageRate,
      row_count: results.reduce((sum, item) => sum + item.row_count, 0),
    },
    warnings: [
      failed ? `${failed} 个页面读取失败，可在下方单独重试。` : "",
      needsReview ? `${needsReview} 个页面质量分低于 70，相关建议需要人工复核。` : "",
      total && results.length < total && !running ? `巡查仅覆盖 ${results.length}/${total} 个页面。` : "",
    ].filter(Boolean),
    results,
  };
}

function renderScanReceipt(receipt = {}) {
  const state = document.getElementById("scan-receipt-state");
  state.className = receipt.readiness || "";

  const summary = receipt.summary || {};
  const results = [...(receipt.results || [])].sort((a, b) => Number(a.ok) - Number(b.ok) || Number(b.needs_review) - Number(a.needs_review));
  const issues = results.filter((item) => !item.ok || item.needs_review);
  const passed = results.filter((item) => item.ok && !item.needs_review);
  state.textContent = receipt.readiness === "running"
    ? receipt.readiness_label || "巡查中"
    : results.length
      ? issues.length ? `${issues.length} 项需处理` : `${passed.length} 项通过`
      : receipt.readiness_label || "等待巡查";
  const metrics = [
    ["覆盖率", `${summary.coverage_rate || 0}%`],
    ["成功页面", `${summary.success || 0}/${summary.total || 0}`],
    ["需复核", summary.needs_review || 0],
    ["读取行数", summary.row_count || 0],
  ];
  const summaryContainer = document.getElementById("scan-receipt-summary");
  summaryContainer.replaceChildren(...metrics.map(([label, value]) => {
    const cell = document.createElement("div");
    const strong = document.createElement("strong"); strong.textContent = String(value);
    const small = document.createElement("small"); small.textContent = label;
    cell.append(strong, small);
    return cell;
  }));

  const warning = document.getElementById("scan-receipt-warning");
  const account = receipt.account_label ? `千川账号：${receipt.account_label}。` : "";
  const finished = receipt.finished_at ? `完成于 ${new Date(receipt.finished_at).toLocaleString()}。` : "";
  warning.textContent = receipt.warnings?.length
    ? `${account}${receipt.warnings.join(" ")}`
    : receipt.readiness === "ready"
      ? `${account}${finished}页面覆盖和质量检查均通过。`
      : receipt.readiness === "running"
        ? "正在生成体检单，巡查完成前不要依据不完整数据调整投放。"
        : "巡查完成后会显示页面覆盖率、数据质量和失败原因。";
  warning.className = `receipt-warning${receipt.readiness === "ready" ? " ready" : ""}`;

  const container = document.getElementById("scan-receipt-pages");
  if (!results.length) return empty(container, "尚未生成数据体检单");
  container.className = "receipt-pages";
  const pageRow = (item) => {
    const cardRow = document.createElement("article");
    cardRow.className = `receipt-page${!item.ok ? " failed" : item.needs_review ? " review" : ""}`;
    const title = document.createElement("strong"); title.textContent = item.label || item.id || "未命名页面";
    const tag = document.createElement("span"); tag.className = "receipt-status";
    tag.textContent = !item.ok ? "失败" : item.needs_review ? "需复核" : "通过";
    const detail = document.createElement("small");
    detail.textContent = !item.ok
      ? item.error || "页面读取失败"
      : `${LABELS[item.source] || item.source} · 质量 ${item.quality_score || 0} · ${item.row_count || 0} 行 · ${item.metric_count || 0} 项指标`;
    cardRow.append(title, tag, detail);
    if (!item.ok && item.id) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.textContent = "只重试这一页";
      retry.addEventListener("click", async () => {
        retry.disabled = true;
        retry.textContent = "正在重试…";
        const response = await chrome.runtime.sendMessage({
          type: "start-full-scan",
          page_ids: [item.id],
          account_key: selectedQianchuanAccount,
        });
        if (!response?.ok) {
          retry.disabled = false;
          retry.textContent = response?.error || "重试失败";
          return;
        }
        await loadDashboard();
      });
      cardRow.append(retry);
    }
    return cardRow;
  };
  const rows = issues.map(pageRow);
  if (!issues.length) {
    const success = document.createElement("p");
    success.className = "receipt-success-note";
    success.textContent = `${passed.length} 个页面均已通过，本轮数据可以用于经营判断。`;
    rows.push(success);
  }
  if (passed.length) {
    const fold = document.createElement("details");
    fold.className = "passed-pages-fold";
    const foldSummary = document.createElement("summary");
    foldSummary.textContent = `查看 ${passed.length} 个已通过页面`;
    const passedList = document.createElement("div");
    passedList.className = "passed-pages-list";
    passedList.replaceChildren(...passed.map(pageRow));
    fold.append(foldSummary, passedList);
    rows.push(fold);
  }
  container.replaceChildren(...rows);
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
  state.textContent = scan.scope === "quick" && scan.status === "completed" ? "首诊断完成" : labels[scan.status] || "未运行";
  state.className = `scan-tag ${running || scan.status === "completed" ? "ok" : ["partial", "interrupted"].includes(scan.status) ? "warn" : scan.status === "error" ? "error" : "idle"}`;
  const total = Number(scan.total || 18);
  const index = Number(scan.index || 0);
  document.getElementById("scan-progress-bar").style.width = `${Math.min(100, total ? index / total * 100 : 0)}%`;
  document.getElementById("scan-detail").textContent = running ? `正在${scan.scope === "quick" ? "首诊断" : "采集"}：${scan.current || "准备中"}（${index}/${total}）` : scan.finished_at ? `${scan.scope === "quick" ? "首诊断" : "上次巡检"}：成功 ${scan.success || 0}，失败 ${scan.failed || 0}` : "按清单自动打开页面并采集，不需要 API";
  const rows = (scan.results || []).reduce((sum, item) => sum + Number(item.quality?.row_count || 0), 0);
  const virtualPasses = (scan.results || []).reduce((sum, item) => sum + Number(item.quality?.virtual_scroll_passes || 0), 0);
  document.getElementById("scan-summary").textContent = scan.error ? `失败原因：${scan.error}` : `成功 ${scan.success || 0} 页，失败 ${scan.failed || 0} 页，低质量 ${scan.low_quality || 0} 页；读取 ${rows} 行，滚动采集 ${virtualPasses} 次`;
  document.getElementById("full-scan-button").disabled = running || !selectedStoreKey;
  document.getElementById("full-scan-button").textContent = running ? "正在自动获取…" : !selectedStoreKey ? "请先选择店铺" : "自动获取全店数据";
  document.getElementById("cancel-scan-button").hidden = !running;
  document.getElementById("retry-scan-button").hidden = running || !(scan.failed > 0);
  renderScanReceipt(scanReceiptFromStatus(scan));
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

function setModuleActionCount(childId, count) {
  const section = document.getElementById(childId)?.closest(".module-section");
  if (section) section.dataset.actionCount = String(Math.max(0, Number(count || 0)));
}

function applyModuleVisibility() {
  document.querySelectorAll(".module-section").forEach((section) => {
    const owners = String(section.dataset.owner || "").split(/\s+/).filter(Boolean);
    section.hidden = !owners.includes(currentRole);
  });
}

function recommendationCard(item, kind) {
  const card = document.createElement("article");
  card.className = `recommendation-card ${item.level || "info"}`;
  const top = document.createElement("div");
  top.className = "recommendation-top";
  const title = document.createElement("strong");
  title.textContent = kind === "plan" ? item.plan : item.product;
  title.title = title.textContent || "";
  const tag = document.createElement("span");
  tag.textContent = kind === "inventory"
    ? item.level === "high" ? "立即补货" : "尽快处理"
    : item.level === "high" ? "立即处理" : item.level === "opportunity" ? "具备放量条件" : "需要关注";
  top.append(title, tag);
  const suggestion = document.createElement("p");
  const suggestionLabel = document.createElement("b");
  suggestionLabel.textContent = kind === "inventory" ? "怎么处理：" : "建议动作：";
  suggestion.append(suggestionLabel, document.createTextNode(item.suggestion || "请回到后台核对后再处理。"));
  const reason = document.createElement("small");
  reason.textContent = `判断依据：${kind === "plan" ? item.reason || "当前投放数据" : item.title || "当前库存数据"}`;
  card.append(top, suggestion, reason);
  appendCopyAction(card, item.action_params);
  return card;
}

function renderPlans(items = []) {
  const container = document.getElementById("plans");
  setModuleActionCount("plans", items.length);
  document.getElementById("plan-count").textContent = `${items.length} 项`;
  if (!items.length) return empty(container, "当前没有投放调整建议；如果尚未巡检，请先同步千川计划和报表。");
  container.className = "stack";
  container.replaceChildren(...items.slice(0, 8).map(planWorkbenchCard));
}

function renderStopLossQueue(report = {}) {
  const items = report.items || [];
  const summary = report.summary || {};
  const container = document.getElementById("stop-loss-queue");
  document.getElementById("stop-loss-count").textContent = `${summary.must_handle || 0} 项必须处理`;
  document.getElementById("stop-loss-summary").textContent =
    `${report.execution_mode_label || "观察模式"} · 预计减少无效消耗 ¥${summary.estimated_savings_low || 0}–¥${summary.estimated_savings_high || 0}。${report.estimate_note || ""}`;
  if (!items.length) return empty(container, "当前没有需要止损的计划；若尚未巡检，请先同步千川计划和报表。");
  container.className = "stack";
  container.replaceChildren(...items.slice(0, 6).map((item) => {
    const card = document.createElement("article");
    card.className = `plan-workbench-card ${item.level || "info"}`;
    const top = document.createElement("div"); top.className = "recommendation-top";
    const title = document.createElement("strong"); title.textContent = item.plan || "千川计划";
    const tag = document.createElement("span"); tag.textContent = `${item.bucket_label} · 风险 ${item.risk_score}`;
    top.append(title, tag);
    const reason = document.createElement("p"); reason.textContent = item.reason || item.diagnosis || "计划需要复核";
    const components = document.createElement("small");
    components.textContent = (item.risk_components || []).map((part) => `${part.label} ${part.score}`).join(" · ");
    const saving = document.createElement("b"); saving.textContent = item.estimated_savings_label || "暂不估算可避免消耗";
    const action = document.createElement("small");
    action.textContent = item.can_start_execution ? "可进入逐次授权的受控执行" : `当前为${report.execution_mode_label || "观察模式"}，先由运营复核`;
    card.append(top, reason, components, saving, action);
    appendCopyAction(card, item.action_params);
    return card;
  }));
}

function renderStrategySimulation(report = {}) {
  const scenarios = report.scenarios || [];
  const selectedPolicy = report.selected_decision?.policy_key;
  const container = document.getElementById("strategy-simulation");
  document.getElementById("strategy-simulation-status").textContent = scenarios.length ? "3 种方案" : "只读预演";
  document.getElementById("strategy-simulation-summary").textContent =
    `${report.recommended_reason || "默认先比较策略影响。"} ${report.note || ""}`;
  if (!scenarios.length) return empty(container, "当前没有足够的止损数据用于策略模拟。");
  container.className = "stack";
  container.replaceChildren(...scenarios.map((scenario) => {
    const card = document.createElement("article");
    card.className = `plan-workbench-card${scenario.key === report.recommended_policy ? " opportunity" : ""}`;
    const top = document.createElement("div"); top.className = "recommendation-top";
    const title = document.createElement("strong"); title.textContent = scenario.label;
    const tag = document.createElement("span");
    tag.textContent = scenario.key === selectedPolicy ? "当前采用" : scenario.key === report.recommended_policy ? "系统建议" : `风险≥${scenario.risk_threshold}`;
    top.append(title, tag);
    const description = document.createElement("p"); description.textContent = scenario.description;
    const metrics = document.createElement("small");
    metrics.textContent = `涉及 ${scenario.selected_plan_count} 个计划 · 预算影响约 ¥${scenario.estimated_budget_impact} · 可避免无效消耗 ¥${scenario.estimated_avoided_waste_low}–¥${scenario.estimated_avoided_waste_high} · 订单风险提示 ${scenario.estimated_orders_at_risk}`;
    const plans = document.createElement("small");
    plans.textContent = scenario.selected_plan_names?.length ? `计划：${scenario.selected_plan_names.join("、")}` : "当前无计划达到该策略阈值";
    const select = document.createElement("button");
    select.type = "button";
    select.className = scenario.key === selectedPolicy ? "small-secondary" : "small-primary";
    select.textContent = scenario.key === selectedPolicy ? "已采用此策略" : "采用此策略";
    select.disabled = scenario.key === selectedPolicy;
    select.addEventListener("click", async () => {
      select.disabled = true;
      select.textContent = "正在记录…";
      try {
        await bridgeFetch("/actions/strategy/select", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
          body: JSON.stringify({ policy_key: scenario.key }),
        });
        await loadDashboard();
      } catch (error) {
        select.disabled = false;
        select.textContent = error.message || "记录失败";
      }
    });
    card.append(top, description, metrics, plans, select);
    return card;
  }));
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
  card.append(top, diagnosis, steps, guardrail);
  appendCopyAction(card, item.action_params);
  card.append(actions);
  return card;
}

function renderInventory(items = []) {
  const container = document.getElementById("inventory");
  const moreWrap = document.getElementById("inventory-more-wrap");
  const moreContainer = document.getElementById("inventory-more");
  const moreCount = document.getElementById("inventory-more-count");
  setModuleActionCount("inventory", items.length);
  document.getElementById("inventory-count").textContent = `${items.length} 项`;
  if (!items.length) {
    moreWrap.hidden = true;
    moreWrap.open = false;
    moreWrap.ontoggle = null;
    moreContainer.replaceChildren();
    return empty(container, "当前没有库存风险；如果刚安装，请先同步商品或库存页面。");
  }
  const priority = { high: 0, warning: 1, info: 2, opportunity: 3 };
  const sorted = [...items].sort((a, b) => (priority[a.level] ?? 9) - (priority[b.level] ?? 9));
  const visible = sorted.slice(0, 4);
  const remaining = sorted.slice(4);
  container.className = "stack inventory-grid";
  container.replaceChildren(...visible.map((item) => recommendationCard(item, "inventory")));
  moreWrap.hidden = remaining.length === 0;
  if (!remaining.length) moreWrap.open = false;
  moreCount.textContent = `${remaining.length} 项`;
  moreContainer.replaceChildren();
  const renderRemaining = () => {
    if (moreWrap.open && !moreContainer.childElementCount) {
      moreContainer.replaceChildren(...remaining.map((item) => recommendationCard(item, "inventory")));
    }
  };
  moreWrap.ontoggle = renderRemaining;
  renderRemaining();
}

function renderCreativeAnalysis(creative = {}) {
  const summary = creative.summary || {};
  const recommendations = creative.recommendations || [];
  const creativeSignals = Number(summary.risky_videos || 0) + Number(summary.untested_videos || 0) + Number(summary.high_potential_videos || 0);
  setModuleActionCount("creative-actions", recommendations.length || creativeSignals);
  document.getElementById("creative-status").textContent = creative.data_status === "ready" ? "分析完成" : "等待数据";
  document.getElementById("creative-count").textContent = `${summary.total_videos || 0} 条`;
  renderMetricStrip("creative-metrics", {
    视频数: summary.total_videos || 0,
    有消耗: summary.spending_videos || 0,
    未测试: summary.untested_videos || 0,
    高风险: summary.risky_videos || 0,
    钩子问题: summary.hook_bottleneck_videos || 0,
    转化问题: summary.conversion_bottleneck_videos || 0,
  });
  document.getElementById("creative-analysis-method").textContent = creative.analysis_method || "展示 → 点击 → 成交 → ROI";
  const matrix = document.getElementById("creative-test-matrix");
  const tests = creative.test_matrix || [];
  if (!tests.length) {
    empty(matrix, "当前没有可生成的内容测试矩阵；请同步包含展示、点击、成交和 ROI 的素材数据。");
  } else {
    matrix.className = "creative-test-matrix";
    matrix.replaceChildren(...tests.map((item) => {
      const card = document.createElement("article"); card.className = "creative-test-card";
      const title = document.createElement("strong"); title.textContent = `${item.label} · ${item.count} 条`;
      const hypothesis = document.createElement("p"); hypothesis.textContent = item.hypothesis || "等待生成测试假设";
      const success = document.createElement("small"); success.textContent = `验收：${item.success_metric || "形成可比较数据"}`;
      card.append(title, hypothesis, success);
      return card;
    }));
  }
  const memory = creative.memory || {};
  document.getElementById("content-memory-status").textContent = memory.verified_pattern_count
    ? `${memory.verified_pattern_count} 条较可信规律`
    : `${memory.observation_count || 0} 条素材经验`;
  document.getElementById("content-memory-note").textContent = memory.note || "内容记忆仅使用当前店铺数据。";
  const memoryContainer = document.getElementById("content-memory-patterns");
  const patterns = memory.patterns || [];
  if (!patterns.length) {
    empty(memoryContainer, "尚未沉淀出可复用规律；继续同步不同素材的完整漏斗数据。");
  } else {
    memoryContainer.className = "content-memory-patterns";
    memoryContainer.replaceChildren(...patterns.map((item) => {
      const card = document.createElement("article"); card.className = `content-memory-pattern ${item.direction || "mixed"}`;
      const title = document.createElement("strong"); title.textContent = `${item.dimension} · ${item.value}`;
      const detail = document.createElement("p");
      detail.textContent = `${item.win_count || 0} 条胜出 / ${item.risk_count || 0} 条风险${item.average_roi == null ? "" : ` · 平均 ROI ${item.average_roi}`}`;
      const confidence = document.createElement("span");
      confidence.textContent = item.confidence === "high" ? "高可信" : item.confidence === "medium" ? "较可信" : "仅作线索";
      card.append(title, detail, confidence);
      return card;
    }));
  }
  renderTasks("creative-actions", recommendations);
  const container = document.getElementById("creative-videos");
  const videos = creative.videos || [];
  if (!videos.length) return empty(container, "暂时没有视频明细；请先同步巨量千川视频库。");
  container.className = "stack";
  container.replaceChildren(...videos.slice(0, 8).map((item) => recommendationCard({
    plan: item.name,
    level: item.level,
    suggestion: item.suggestion,
    action_params: item.action_params,
    reason: `${item.status} · 消耗 ${item.evidence?.spend == null ? "--" : item.evidence.spend} · ROI ${item.evidence?.roi == null ? "--" : item.evidence.roi}`,
  }, "plan")));
}

function renderValueLedger(ledger = {}) {
  const summary = ledger.summary || {};
  const evaluated = Number(summary.evaluated_actions || 0);
  document.getElementById("value-ledger-status").textContent = evaluated
    ? `${summary.effective_actions || 0}/${evaluated} 项有效`
    : `${summary.waiting_review || 0} 项待复查`;
  renderMetricStrip("value-ledger-metrics", {
    已完成任务: summary.completed_tasks || 0,
    等待复盘: summary.tasks_waiting_review || 0,
    已验收动作: summary.verified_actions || 0,
    已完成复盘: evaluated,
    有效率: summary.effective_rate == null ? "--" : `${summary.effective_rate}%`,
    受控预算幅度: `¥${summary.protected_budget_capacity || 0}`,
  });
  document.getElementById("value-ledger-note").textContent = ledger.note || "价值账本只记录已回读、可复核的数据。";
}

function taskModuleTarget(item = {}) {
  const context = `${item.title || ""} ${item.action || ""} ${item.suggestion || ""}`;
  if (/(库存|补货|断货|可售)/.test(context)) return "inventory";
  if (/(素材|视频|创意)/.test(context)) return "creative-actions";
  if (/(直播|进房|场次|开播)/.test(context)) return "live-actions";
  if (/(货架|主图|标题|搜索|推荐卡|商城)/.test(context)) return "shelf-actions";
  if (/(投放|千川|计划|ROI|消耗|预算|出价)/i.test(context)) return "plans";
  return {
    货架运营: "shelf-actions",
    直播运营: "live-actions",
    投放运营: "plans",
    商品运营: "inventory",
  }[item.owner] || "";
}

function taskBusinessRole(item = {}) {
  const context = `${item.title || ""} ${item.action || ""} ${item.suggestion || ""} ${item.evidence || ""}`;
  if (/(素材|视频|创意|内容|脚本|话术|钩子|口播|封面)/.test(context)) return "内容";
  if (/(库存|补货|断货|可售|货架|主图|标题|搜索|推荐卡|商城|商品卡)/.test(context)) return "货架商品";
  if (/(直播|进房|场次|开播|投放|千川|计划|ROI|消耗|预算|出价)/i.test(context)) return "直播投放";
  return {
    货架运营: "货架商品",
    商品运营: "货架商品",
    直播运营: "直播投放",
    投放运营: "直播投放",
  }[item.owner] || "货架商品";
}

function taskBelongsToCurrentRole(item = {}) {
  return taskBusinessRole(item) === currentRole;
}

function revealModuleByChildId(targetId) {
  const target = targetId ? document.getElementById(targetId) : null;
  const section = target?.closest(".module-section");
  if (!section) return false;
  section.hidden = false;
  section.classList.remove("module-highlight");
  requestAnimationFrame(() => {
    section.classList.add("module-highlight");
    section.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  setTimeout(() => section.classList.remove("module-highlight"), 1800);
  return true;
}

function jumpToTaskModule(item, button) {
  if (!revealModuleByChildId(taskModuleTarget(item))) return;
  button.textContent = "已定位到详情";
  setTimeout(() => {
    button.textContent = "查看对应模块 ↓";
  }, 1800);
}

function taskCard(item, options = {}) {
  const card = document.createElement("article");
  card.className = `task-card ${item.level || "info"}`;
  const meta = document.createElement("div");
  meta.className = "task-meta";
  const owner = taskBusinessRole(item);
  const queuePrefix = options.queueIndex ? `第 ${options.queueIndex} 项 · ` : "";
  meta.textContent = `${queuePrefix}${item.level === "high" ? "立即处理" : item.level === "opportunity" ? "增长机会" : "今日处理"} · ${owner || "运营"}`;
  const title = document.createElement("strong"); title.textContent = item.title || "运营任务";
  const assignee = document.createElement("div"); assignee.className = "task-assignee";
  assignee.textContent = `负责人：${item.assignee || item.owner || "待分配"}${item.last_operator ? ` · 最近操作：${item.last_operator}` : ""}`;
  const action = document.createElement("p");
  const actionLabel = document.createElement("b"); actionLabel.textContent = "下一步：";
  action.append(actionLabel, document.createTextNode(item.action || item.suggestion || "请先回到后台核对数据。"));
  const chips = document.createElement("div"); chips.className = "task-chips";
  const trustLabel = currentOperationContext?.state === "ready"
    ? "当前数据可信"
    : currentOperationContext?.state === "blocked"
      ? "经营判断已暂停"
      : "建议需人工复核";
  [item.impact, item.confidence === "high" ? "证据较充分" : "证据需复核", trustLabel].filter(Boolean).forEach((value) => {
    const chip = document.createElement("span"); chip.textContent = value; chips.append(chip);
  });
  const detail = document.createElement("details"); detail.className = "task-detail";
  const detailSummary = document.createElement("summary"); detailSummary.textContent = "为什么这样建议？";
  const evidence = document.createElement("small"); evidence.textContent = `数据依据：${item.evidence || "当前页面数据"}`;
  const acceptance = document.createElement("small"); acceptance.textContent = `完成后检查：${item.acceptance || "确认后台数据已经更新"}`;
  detail.append(detailSummary, evidence, acceptance);
  card.append(meta, title, assignee, action, chips, detail);
  appendCopyAction(card, item.action_params);
  if (item.id) {
    const actions = document.createElement("div"); actions.className = "task-actions";
    const statusLabel = document.createElement("span");
    const labels = { todo: "待处理", doing: "进行中", observing: "等待复盘", blocked: "已阻止", done: "已完成" };
    statusLabel.textContent = labels[item.status] || "待处理";
    const transitions = item.status === "todo" ? [["开始处理", "doing"], ["转交", "transfer"], ["阻止", "blocked"]]
      : item.status === "doing" ? [["等待复盘", "observing"], ["完成", "done"], ["转交", "transfer"], ["阻止", "blocked"]]
      : item.status === "observing" ? [["完成", "done"], ["继续处理", "doing"], ["转交", "transfer"], ["阻止", "blocked"]]
      : item.status === "blocked" ? [["解除阻止", "todo"], ["转交", "transfer"]]
      : [["重新打开", "todo"]];
    actions.append(statusLabel);
    transitions.forEach(([label, status]) => {
      const button = document.createElement("button"); button.textContent = label; button.setAttribute("aria-label", `${label}：${item.title || '任务'}`);
      button.addEventListener("click", async () => {
        button.disabled = true;
        let nextStatus = status;
        let nextAssignee = "";
        let note = "";
        if (status === "transfer") {
          nextAssignee = window.prompt("转交给谁？请输入负责人姓名或岗位", item.assignee || item.owner || "")?.trim() || "";
          if (!nextAssignee) { button.disabled = false; return; }
          nextStatus = item.status || "todo";
        }
        if (status === "blocked") {
          note = window.prompt("为什么阻止这项任务？请填写恢复处理前必须解决的问题", item.blocked_reason || "")?.trim() || "";
          if (!note) { button.disabled = false; return; }
        }
        // When starting a task, save a suggestion snapshot for effectiveness tracking
        if (nextStatus === "doing" && item.status === "todo") {
          bridgeFetch("/tasks/track", { method: "POST", headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" }, body: JSON.stringify({ task_id: item.id, context: { title: item.title, owner: item.owner } }) }).catch(() => undefined);
        }
        await bridgeFetch("/tasks/update", { method: "POST", headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" }, body: JSON.stringify({
          task_id: item.id,
          status: nextStatus,
          operator: currentRole,
          assignee: nextAssignee,
          note,
          title: item.title,
          owner: item.owner,
          store_key: selectedStoreKey,
        }) });
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
    const fbDefer = document.createElement("button"); fbDefer.className = "fb-btn"; fbDefer.textContent = "稍后处理"; fbDefer.setAttribute("aria-label", `暂不处理"${item.title || '建议'}"`);
    const fbStatus = document.createElement("small"); fbStatus.className = "fb-status";
    [fbUp, fbDown, fbDefer].forEach((btn, index) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try {
          await bridgeFetch("/feedback", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
            body: JSON.stringify({ task_id: item.id, rating: index === 0 ? "up" : index === 1 ? "down" : "defer", context: item.title || "" }),
          });
          fbStatus.textContent = index === 2 ? "已记为稍后处理" : "感谢反馈";
          fbUp.disabled = true; fbDown.disabled = true; fbDefer.disabled = true;
        } catch { fbStatus.textContent = "反馈失败"; btn.disabled = false; }
      });
    });
    feedback.append(fbLabel, fbUp, fbDown, fbDefer, fbStatus);
    card.append(feedback);
  }
  if (options.showModuleLink && taskModuleTarget(item)) {
    const jump = document.createElement("button");
    jump.type = "button";
    jump.className = "module-jump-button";
    jump.textContent = "查看对应模块 ↓";
    jump.setAttribute("aria-label", `查看“${item.title || "任务"}”对应的详细经营模块`);
    jump.addEventListener("click", () => jumpToTaskModule(item, jump));
    card.append(jump);
  }
  return card;
}

function renderTasks(id, items = [], options = {}) {
  const container = document.getElementById(id);
  if (!items.length) return empty(container, "当前没有需要处理的任务；如果数据未同步，请先完成一次巡检。");
  container.className = "stack";
  container.replaceChildren(...items.slice(0, 8).map((item, index) => taskCard(item, {
    ...options,
    queueIndex: options.queue ? index + 1 : null,
  })));
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
  const levelPriority = { high: 0, warning: 1, info: 2, opportunity: 3 };
  const statusPriority = { doing: 0, todo: 1, observing: 2 };
  return source
    .filter((item) => item.status !== "done" && taskBelongsToCurrentRole(item) && (opportunity ? item.level === "opportunity" : item.level !== "opportunity"))
    .sort((a, b) => {
      const levelDelta = (levelPriority[a.level] ?? 9) - (levelPriority[b.level] ?? 9);
      if (levelDelta) return levelDelta;
      return (statusPriority[a.status] ?? 9) - (statusPriority[b.status] ?? 9);
    });
}

function renderQueueStats(items = []) {
  const container = document.getElementById("manager-queue-stats");
  const stats = [
    ["紧急", items.filter((item) => item.level === "high").length, "urgent"],
    ["待开始", items.filter((item) => !item.status || item.status === "todo").length, "todo"],
    ["进行中", items.filter((item) => item.status === "doing").length, "doing"],
    ["待复盘/阻止", items.filter((item) => ["observing", "blocked"].includes(item.status)).length, "blocked"],
  ];
  container.replaceChildren(...stats.map(([label, value, tone]) => {
    const item = document.createElement("div");
    item.className = `queue-stat ${tone}`;
    const strong = document.createElement("strong"); strong.textContent = value;
    const small = document.createElement("small"); small.textContent = label;
    item.append(strong, small);
    return item;
  }));
}

function renderNextBestAction(items = []) {
  const panel = document.getElementById("next-best-action");
  const title = document.getElementById("next-best-action-title");
  const detail = document.getElementById("next-best-action-detail");
  const button = document.getElementById("next-best-action-button");
  const next = items.find((item) => item.status === "doing") || items[0];
  panel.classList.toggle("empty", !next);
  panel.classList.toggle("urgent", next?.level === "high");
  if (!next) {
    title.textContent = "当前岗位没有待处理事项";
    detail.textContent = "可以检查增长机会，或同步最新数据生成下一轮建议。";
    button.textContent = "查看增长机会";
    button.dataset.target = "growth-section";
    return;
  }
  title.textContent = next.title || "处理当前最高优先级任务";
  detail.textContent = next.action || next.acceptance || next.evidence || "打开处置队列查看依据和验收标准。";
  button.textContent = next.status === "doing" ? "继续处理" : "开始处理";
  button.dataset.target = "manager-tasks";
}

function renderPriorityReminder() {
  const panel = document.getElementById("priority-reminder");
  const title = document.getElementById("priority-reminder-title");
  const detail = document.getElementById("priority-reminder-detail");
  const button = document.getElementById("priority-reminder-action");
  const context = currentOperationContext || {};
  const tasks = currentOps ? roleTasks(currentOps, false) : [];
  const urgent = tasks.find((item) => item.level === "high");
  const blockers = context.blockers || [];
  if (context.state === "blocked" || blockers.length) {
    panel.hidden = false;
    panel.className = "priority-reminder";
    title.textContent = context.state === "blocked" ? "经营数据未准备好，暂时不要直接做投放决策" : "重要数据需要复核";
    detail.textContent = context.next_action || blockers[0] || "请先完成一次全店巡检并核对当前店铺。";
    button.textContent = context.selected_store?.key ? "立即补齐数据" : "选择并绑定店铺";
    button.dataset.mode = "data";
    return;
  }
  if (context.state === "review") {
    panel.hidden = false;
    panel.className = "priority-reminder review";
    title.textContent = "经营数据可用，部分建议需要人工复核";
    detail.textContent = context.next_action || "纯抖店巡店和第一条诊断可以继续；资金与投放动作仍不会自动执行。";
    button.textContent = "查看数据体检";
    button.dataset.mode = "review";
    return;
  }
  if (urgent) {
    panel.hidden = false;
    panel.className = "priority-reminder";
    title.textContent = `紧急：${urgent.title || "处理今日高风险事项"}`;
    detail.textContent = urgent.action || urgent.evidence || "请优先完成该任务，再处理其他经营事项。";
    button.textContent = urgent.status === "doing" ? "继续处理" : "立即处理";
    button.dataset.mode = "task";
    return;
  }
  if (!currentOps || context.state === "checking") {
    panel.hidden = false;
    panel.className = "priority-reminder checking";
    title.textContent = "正在检查今天最重要的经营事项";
    detail.textContent = "完成数据核对后，只在这里保留需要立刻关注的提醒。";
    button.textContent = "正在检查";
    button.dataset.mode = "checking";
    return;
  }
  panel.hidden = true;
}

function renderOperations(ops, shelf, live, creative, coverage = []) {
  currentOps = ops;
  currentOperationsContext = { ops, shelf, live, creative, coverage };
  const allTasks = roleTasks(ops, false);
  const allGrowth = roleTasks(ops, true);
  const visibleTasks = managerQueueExpanded ? allTasks : allTasks.slice(0, 3);
  const expand = document.getElementById("manager-expand");
  document.getElementById("task-heading").textContent = `${currentRole} · 今日处置队列`;
  document.getElementById("manager-queue-caption").textContent = "按紧急程度排列，完成动作后再进入观察。";
  document.getElementById("manager-count").textContent = `${allTasks.length} 项待处理`;
  expand.hidden = allTasks.length <= 3;
  expand.textContent = managerQueueExpanded ? "收起队列" : `查看全部 ${allTasks.length} 项`;
  renderQueueStats(allTasks);
  renderNextBestAction(allTasks);
  renderPriorityReminder();
  renderTasks("manager-tasks", visibleTasks, { queue: true, showModuleLink: true });
  document.getElementById("growth-count").textContent = `${allGrowth.length} 项`;
  renderTasks("growth-tasks", allGrowth.slice(0, 3), { showModuleLink: true });
  const scoped = (ops.all_tasks || []).filter(taskBelongsToCurrentRole);
  const done = scoped.filter((item) => item.status === "done").length;
  document.getElementById("progress-rate").textContent = scoped.length ? `${Math.round(done / scoped.length * 100)}%` : "--";
  document.getElementById("doing-count").textContent = scoped.filter((item) => item.status === "doing").length;
  document.getElementById("observing-count").textContent = scoped.filter((item) => item.status === "observing").length;
  const fresh = coverage.filter((item) => item.fresh).length;
  document.getElementById("data-freshness").textContent = coverage.length ? `${fresh}/${coverage.length}` : "--";
  document.getElementById("shelf-status").textContent = shelf.data_status === "ready" ? "分析完成" : "等待数据";
  renderMetricStrip("shelf-metrics", { 曝光: shelf.funnel?.exposure, 点击: shelf.funnel?.clicks, 成交人数: shelf.funnel?.buyers, 点击率: shelf.funnel?.click_rate == null ? null : `${shelf.funnel.click_rate.toFixed(1)}%` });
  const shelfRecommendations = shelf.recommendations || [];
  setModuleActionCount("shelf-actions", shelfRecommendations.length);
  renderTasks("shelf-actions", shelfRecommendations);
  document.getElementById("live-status").textContent = live.data_status === "ready" ? "分析完成" : "等待数据";
  renderMetricStrip("live-metrics", { 进房: live.funnel?.views, 进房率: live.funnel?.enter_rate == null ? null : `${live.funnel.enter_rate.toFixed(1)}%`, 商品点击: live.funnel?.product_clicks, 订单: live.funnel?.orders, ROI: live.funnel?.roi });
  const liveRecommendations = live.recommendations || [];
  setModuleActionCount("live-actions", liveRecommendations.length);
  renderTasks("live-actions", liveRecommendations);
  renderCreativeAnalysis(creative || {});
  applyModuleVisibility();
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
  document.getElementById("execution-mode").value = settings.execution_mode || "observe";
  document.getElementById("roi-target").value = settings.roi_target;
  document.getElementById("spend-threshold").value = settings.min_spend_for_action;
  document.getElementById("stock-threshold").value = settings.low_inventory_threshold;
  document.getElementById("daily-execution-limit").value = settings.max_daily_execution_count ?? 3;
  document.getElementById("daily-budget-limit").value = settings.max_daily_budget_reduction ?? 300;
  document.getElementById("execution-cooldown").value = settings.execution_cooldown_minutes ?? 30;
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
  const analysisStoreKey = String(payload.selected_store_key || "");
  // The Agent setting is the only source of truth. Never silently fall back to
  // another store because a stale browser preference can mix operational context.
  const activeKey = accounts.some((account) => account.key === analysisStoreKey)
    ? analysisStoreKey
    : "";
  const analysisAccount = accounts.find((account) => account.key === activeKey);
  select.replaceChildren();
  const current = document.createElement("option");
  current.value = "";
  current.textContent = accounts.length ? "请选择当前店铺" : "尚未识别店铺";
  select.append(current);
  accounts.forEach((account) => {
    const option = document.createElement("option");
    option.value = account.key;
    const accountLabel = account.label || `匿名店铺 ${String(account.key || "").slice(-4).toUpperCase()}`;
    const stateLabel = account.state_label || (account.channel === "official_api" ? "官方 API" : "网页");
    option.textContent = `${accountLabel} · ${stateLabel}`;
    select.append(option);
  });
  selectedStoreKey = activeKey;
  selectedQianchuanAccount = String(payload.selected_account_key || "");
  select.value = activeKey;
  chrome.storage.local.set({ scanStorePreference: activeKey, scanAccountPreference: selectedQianchuanAccount });
  accountSelectionRequired = !activeKey;
  const scanButton = document.getElementById("full-scan-button");
  scanButton.disabled = !activeKey;
  scanButton.title = activeKey ? "按当前店铺开始巡检" : "请先选择当前店铺";
  document.getElementById("active-store-name").textContent = analysisAccount?.label || "尚未识别店铺";
  document.getElementById("store-mode-summary").textContent = analysisAccount
    ? `${payload.store_count || accounts.length} 个店铺 · 当前${analysisAccount.state_label || "数据已隔离"} · 建议与日志仅使用本店数据`
    : "请先打开抖店经营概览完成匿名店铺识别；千川不是首次使用的必选项。";
  document.getElementById("qianchuan-account-hint").textContent = analysisAccount
    ? selectedQianchuanAccount
      ? `当前巡检固定为“${analysisAccount.label}”，并使用已确认关联的匿名千川账户。`
      : `当前巡检固定为“${analysisAccount.label}”；先使用抖店数据，千川可稍后关联。`
    : "尚未选择店铺，本次不会启动跨页面巡检。";
  const linkReview = document.getElementById("store-link-review");
  const unlinked = payload.unlinked_accounts || [];
  linkReview.hidden = !(activeKey && unlinked.length);
  const unlinkedSelect = document.getElementById("unlinked-account-select");
  unlinkedSelect.replaceChildren(...unlinked.map((account) => {
    const option = document.createElement("option");
    option.value = account.key;
    option.textContent = account.label || `匿名千川账户 ${String(account.key || "").slice(-4).toUpperCase()}`;
    return option;
  }));
}

const PROMOTION_MODE_LABELS = {
  standard: "标准计划",
  full_domain: "全域推广",
  chengfang: "千川乘方",
  unknown: "尚未确认",
};

const PROMOTION_CONFIDENCE_LABELS = {
  high: "高可信",
  medium: "中等可信",
  low: "低可信",
  conflict: "证据冲突",
  unknown: "待验证",
};

function chengfangDisplayValue(field, formatter = (value) => String(value)) {
  if (!field || field.status !== "present" || field.value === null || field.value === undefined) return "待同步";
  return formatter(field.value);
}

function renderChengfangReadiness(report = {}) {
  const dashboard = report.dashboard || {};
  const summary = report.summary || {};
  const mode = dashboard.mode || {};
  const scope = dashboard.scope || {};
  const metric = dashboard.metric_contract || {};
  const quality = dashboard.data_quality_gate || {};
  const observed = quality.observed || {};
  const strategy = dashboard.strategy || {};
  const snapshot = report.snapshot || {};
  const contract = report.field_contract || {};
  const activeMode = mode.value || summary.promotion_mode || "unknown";
  const isChengfang = activeMode === "chengfang";
  const tag = document.getElementById("promotion-mode-readonly-tag");
  tag.textContent = isChengfang ? "乘方 · 只读" : activeMode === "unknown" ? "模式待确认 · 禁止写入" : `${PROMOTION_MODE_LABELS[activeMode] || "投放"} · 只读核验`;
  tag.className = `promotion-status ${isChengfang ? "danger" : activeMode === "unknown" ? "warning" : "safe"}`;
  document.getElementById("chengfang-summary").textContent = isChengfang
    ? "已识别乘方模式；先完成真实字段与指标口径验真，再生成经营建议。"
    : "尚未取得可信乘方模式证据；当前页面不会展示猜测的预算或 ROI。";

  const freshness = observed.freshness_seconds == null
    ? "待同步"
    : observed.freshness_seconds === 0 ? "0 秒" : `${Math.ceil(observed.freshness_seconds / 60)} 分钟前`;
  const completeness = typeof observed.completeness === "number" ? `${Math.round(observed.completeness * 100)}%` : "待同步";
  const cards = [
    ["投放模式", PROMOTION_MODE_LABELS[activeMode] || "尚未确认", mode.conflict ? "danger" : isChengfang ? "safe" : "warning", PROMOTION_CONFIDENCE_LABELS[mode.confidence || summary.mode_confidence] || "待验证"],
    ["账户绑定", scope.complete ? "已确认" : scope.conflict ? "存在冲突" : "待同步", scope.complete ? "safe" : scope.conflict ? "danger" : "warning", scope.complete ? "店铺与千川账户作用域完整" : "不完整时禁止任何写操作"],
    ["超级策略", chengfangDisplayValue(strategy.strategy_id), strategy.strategy_id?.status === "present" ? "safe" : "warning", "策略 ID 未确认时不生成策略动作"],
    ["综合 ROI 口径", metric.definition && metric.definition !== "unknown" && metric.version ? metric.name || metric.definition : "暂不可用", metric.definition && metric.definition !== "unknown" && metric.version ? "safe" : "danger", metric.version ? `口径版本 ${metric.version}` : "必须确认分子、分母和退款归因"],
    ["数据新鲜度", freshness, observed.freshness_seconds != null && observed.freshness_seconds <= 1800 ? "safe" : "warning", `完整度 ${completeness}`],
    ["成本 / 结果", dashboard.profit_safety?.calculable ? "可计算利润" : "待补齐", dashboard.profit_safety?.calculable ? "safe" : "warning", dashboard.profit_safety?.calculable ? "允许生成只读利润诊断" : "不展示猜测的利润、预算或 ROI"],
    ["字段合同", contract.verified ? "已验证" : "暂不可用", contract.verified ? "safe" : "danger", contract.verified ? `版本 ${contract.contract_version}` : "尚未验证真实乘方字段"],
    ["最近快照", snapshot.available ? snapshot.saved_at || "已同步" : "待同步", snapshot.available ? "safe" : "warning", snapshot.page_type ? `页面 ${snapshot.page_type}` : "请打开乘方页面后同步"],
  ];
  const grid = document.getElementById("chengfang-status-grid");
  grid.replaceChildren(...cards.map(([label, value, level, detail]) => {
    const card = document.createElement("article");
    card.className = `chengfang-status-card ${level}`;
    const small = document.createElement("small"); small.textContent = label;
    const strong = document.createElement("strong"); strong.textContent = value;
    const p = document.createElement("p"); p.textContent = detail;
    card.append(small, strong, p);
    return card;
  }));

  const blockers = [...(report.blockers || [])];
  if (!contract.verified) blockers.push(...(contract.blockers || []));
  const uniqueBlockers = [...new Set(blockers.filter(Boolean))];
  document.getElementById("chengfang-blocker-count").textContent = `${uniqueBlockers.length} 项`;
  const blockerList = document.getElementById("chengfang-blockers-list");
  blockerList.replaceChildren(...(uniqueBlockers.length ? uniqueBlockers : ["只读数据已就绪；乘方写操作仍保持关闭。"])
    .map((message) => { const li = document.createElement("li"); li.textContent = message; return li; }));
  document.getElementById("chengfang-next-step").textContent = report.next_step || dashboard.next_step || "同步真实乘方页面并完成字段验真。";
}

function renderChengfangUnavailable(message) {
  renderChengfangReadiness({
    summary: { promotion_mode: "unknown", mode_confidence: "unknown" },
    blockers: [message || "乘方准备度暂时无法读取。"],
    next_step: "确认本地 Agent 已启动，然后重新同步当前千川页面。",
  });
}

function renderOperationContext(payload = {}) {
  currentOperationContext = payload;
  renderPriorityReminder();
}

function renderOnboarding(payload = {}) {
  currentOnboarding = payload;
}

function renderConnectionGuide(payload = {}, catalog = {}) {
  currentConnectionGuide = payload;
  const guide = document.getElementById("connection-guide");
  const guideView = globalThis.DianConnectionGuidePolicy.guideView(payload, { qianchuanDeferred: qianchuanFeatureDeferred });
  const collapsed = guideView.collapsed;
  guide.className = `connection-guide ${collapsed ? "collapsed" : "expanded"}`;
  document.getElementById("connection-status-strip").hidden = !collapsed;
  document.getElementById("connection-guide-expanded").hidden = collapsed;
  document.getElementById("connection-level").textContent = `${payload.level || "L0"} · ${payload.level_label || "尚未连接店铺"}`;
  document.getElementById("connection-level-compact").textContent = `${payload.level || "L0"} · ${payload.level_label || "尚未连接"}`;
  const updatedAt = Number(payload.store?.updated_at || 0);
  document.getElementById("connection-update-time").textContent = updatedAt
    ? `数据更新于 ${new Date(updatedAt * 1000).toLocaleString()}` : "暂无更新时间";
  const levels = payload.levels || [];
  document.getElementById("connection-levels").replaceChildren(...levels.map((level) => {
    const item = document.createElement("div");
    item.className = level.reached ? "reached" : "pending";
    item.setAttribute("aria-label", `${level.id} ${level.label}，${level.reached ? "已达到" : "未达到"}`);
    const marker = document.createElement("strong"); marker.textContent = level.reached ? "✓" : "○";
    const copy = document.createElement("span"); copy.textContent = `${level.id} ${level.label}`;
    item.append(marker, copy);
    return item;
  }));
  const next = payload.next_upgrade || {};
  document.getElementById("connection-next-title").textContent = next.label || "正在检查连接状态";
  document.getElementById("connection-next-detail").textContent = next.failure || payload.note || "按提示完成当前步骤。";
  document.getElementById("connection-eta").textContent = `预计：${next.eta || "约 1 分钟"}`;
  document.getElementById("connection-value").textContent = next.value || "完成后继续下一步";
  const action = document.getElementById("connection-guide-action");
  action.dataset.action = guideView.actionId;
  action.textContent = next.label || "正在检查";
  action.disabled = !next.id;
  const failure = document.getElementById("connection-failure-help");
  failure.hidden = !next.failure;
  failure.textContent = next.failure || "";
  const storeControls = document.getElementById("connection-store-controls");
  storeControls.hidden = !["confirm_store", "select_store"].includes(next.id);
  document.getElementById("current-qianchuan-button").hidden = next.id !== "sync_qianchuan";
  document.getElementById("connection-skip-qianchuan").hidden = next.id !== "sync_qianchuan";
  if (guideView.deferred) {
    document.getElementById("connection-skip-qianchuan").textContent = "已暂不使用，可随时再连接";
  }
  document.getElementById("connection-tutorial-steps").replaceChildren(...(payload.tutorial || []).map((step) => {
    const item = document.createElement("li");
    item.className = step.complete ? "complete" : "pending";
    item.textContent = `${step.complete ? "已完成" : step.optional ? "可选" : "待完成"} · ${step.label}：${step.detail || ""}`;
    return item;
  }));
  if (!catalog.store_count && next.id === "identify_store") storeControls.hidden = true;
}

function renderTodayFocus(ops = {}) {
  const focus = ops.today_focus || {};
  const topThree = focus.top_three || [];
  document.getElementById("today-three-summary").textContent = topThree.length
    ? topThree.map((item, index) => `${index + 1}. ${item.title || "经营任务"}`).join(" · ")
    : "当前没有待处理任务";
  document.getElementById("today-max-risk").textContent = focus.max_risk?.title || "当前没有高风险事项";
  const yesterday = focus.yesterday_result;
  document.getElementById("yesterday-action-result").textContent = yesterday
    ? `${yesterday.status_label || "已复盘"} · ${yesterday.plan_name || "受控动作"}`
    : "昨日暂无已完成复盘动作";
  const unsynced = focus.unsynced_data || [];
  document.getElementById("unsynced-data-summary").textContent = unsynced.length
    ? `${unsynced.length} 项：${unsynced.slice(0, 2).map((item) => item.label).join("、")}`
    : "关键经营数据已同步";
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
  const automationSurface = globalThis.DianConnectionGuidePolicy.automationSurface({
    selectedAccountKey: selectedQianchuanAccount,
    itemCount: items.length,
    deferred: qianchuanFeatureDeferred,
  });
  const qianchuanConnected = ["candidates", "no_plans"].includes(automationSurface);
  const offState = document.getElementById("automation-off-state");
  const workflow = document.getElementById("automation-workflow");
  offState.hidden = qianchuanConnected;
  workflow.hidden = !qianchuanConnected;
  if (!qianchuanConnected) {
    const heading = offState.querySelector("strong");
    const detail = offState.querySelector("p");
    const skip = document.getElementById("automation-skip");
    if (automationSurface === "deferred") {
      heading.textContent = "已暂不使用投放功能";
      detail.textContent = "抖店巡店与经营诊断会继续正常使用；需要投放时，再同步当前千川页即可开启。";
      skip.textContent = "已跳过";
      skip.disabled = true;
    } else {
      heading.textContent = "投放自动化尚未开启";
      detail.textContent = "连接千川后，我们会找到可以止损、观察或放量的计划。纯抖店巡店不受影响。";
      skip.textContent = "暂不使用投放功能";
      skip.disabled = false;
    }
  }
  document.getElementById("shadow-actions")?.closest(".module-section")?.toggleAttribute("hidden", !qianchuanConnected);
  document.getElementById("plans")?.closest(".module-section")?.toggleAttribute("hidden", !qianchuanConnected);
  if (!qianchuanConnected) {
    document.getElementById("automation-status").textContent = "尚未开启 · 不影响抖店巡店";
    setModuleActionCount("automation-candidates", 0);
    applyModuleVisibility();
    document.getElementById("shadow-actions")?.closest(".module-section")?.toggleAttribute("hidden", true);
    document.getElementById("plans")?.closest(".module-section")?.toggleAttribute("hidden", true);
    return;
  }
  const totalActionable = Number(summary.preflight_ready || 0) + Number(summary.confirmable || 0) + Number(summary.blocked || 0);
  setModuleActionCount("automation-candidates", items.length);
  applyModuleVisibility();
  document.getElementById("automation-status").textContent = summary.preflight_ready
    ? `${summary.preflight_ready} 项等你确认`
    : summary.confirmable
      ? `${summary.confirmable} 项可以继续`
      : items.length ? "需要补数据" : "已连接，等待计划数据";
  renderMetricStrip("automation-summary", {
    等你确认: summary.preflight_ready || 0,
    可以继续: summary.confirmable || 0,
    需要补数据: summary.blocked || 0,
    请手动处理: summary.manual_only || 0,
  });

  const criteria = document.getElementById("automation-criteria-list");
  criteria.replaceChildren(...(report.criteria || []).map((value) => {
    const item = document.createElement("li");
    item.textContent = value;
    return item;
  }));

  const container = document.getElementById("automation-candidates");
  if (!items.length) return empty(container, "千川账户已经连接，但当前页还没有发现可分析的计划。请打开一个计划页并同步当前千川页。");
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
      const needsReread = (item.blocked_reasons || []).some((reason) => ["DATA_STALE", "CAPTURE_TIME_MISSING", "DATA_QUALITY_LOW", "CONFIDENCE_NOT_HIGH"].includes(reason.code));
      button.textContent = needsReread ? "重新读取当前千川页" : item.status === "confirmable" ? "查看并确认方案" : item.status === "preflight_ready" ? "启动执行前检查" : "查看投放方案";
      button.addEventListener("click", async () => {
        if (needsReread) {
          document.getElementById("current-qianchuan-button").click();
          document.querySelector(".scan-card")?.scrollIntoView({ behavior: "smooth", block: "start" });
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
  const stages = [...document.querySelectorAll("[data-automation-step]")];
  stages.forEach((stage) => stage.classList.remove("done", "current"));
  const stageMap = Object.fromEntries(stages.map((stage) => [stage.dataset.automationStep, stage]));
  const activeStep = globalThis.DianConnectionGuidePolicy.automationStep(state);
  if (activeStep === "proposal") {
    stageMap.proposal?.classList.add("current");
  } else if (activeStep === "authorization") {
    stageMap.proposal?.classList.add("done");
    stageMap.authorization?.classList.add("current");
  } else {
    stageMap.proposal?.classList.add("done");
    stageMap.authorization?.classList.add("done");
    stageMap.result?.classList.add("current");
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

function formatVersion(value, prefix = "v") {
  const text = String(value || "").trim();
  return text ? `${prefix}${text.replace(/^v/i, "")}` : "暂无";
}

function browserFamily() {
  const agent = navigator.userAgent || "";
  if (/Edg\//.test(agent)) return "edge";
  if (/QQBrowser\//.test(agent)) return "qq";
  if (/360(?:SE|EE)|QIHU/i.test(agent)) return "360";
  return /Chrome\//.test(agent) ? "chrome" : "unknown";
}

async function reportExtensionInstallSource() {
  const manifest = chrome.runtime.getManifest();
  const browser = browserFamily();
  const storeSources = {
    chrome: "chrome_web_store",
    edge: "edge_addons",
    "360": "360_extension_store",
  };
  const source = manifest.update_url ? (storeSources[browser] || "chrome_web_store") : "unpacked";
  return bridgeFetch("/distribution/extension-source", {
    method: "POST",
    body: JSON.stringify({
      source,
      browser,
      version: manifest.version,
      extension_id: chrome.runtime.id || "",
    }),
  });
}

function renderReleaseCheck(checks, id, stateId, noteId, readyText, blockedText) {
  const check = checks.find((item) => item?.id === id) || {};
  const state = document.getElementById(stateId);
  state.className = `status-dot ${check.ready ? "ready" : "blocked"}`;
  document.getElementById(noteId).textContent = check.ready ? readyText : blockedText;
}

function renderSystemStatus(system = {}) {
  const database = system.database || {};
  const knowledge = system.knowledge || {};
  const update = system.update || {};
  const scan = system.scan || {};
  const telemetry = system.telemetry || {};
  const localQueue = telemetry.local_queue || {};
  const distribution = system.distribution || {};
  const extensionDistribution = distribution.extension || {};
  const release = system.release_readiness || {};
  const runtime = system.runtime || {};
  const checks = Array.isArray(release.checks) ? release.checks : [];
  const manifestVersion = chrome.runtime.getManifest()?.version || "";
  const versionCompatible = !system.required_extension_version || manifestVersion === system.required_extension_version;
  document.getElementById("agent-version").textContent = formatVersion(system.agent_version);
  document.getElementById("agent-version-note").textContent = runtime.state === "healthy"
    ? (runtime.last_recovery_at ? "自动启动正常 · 最近已自愈" : "自动启动与保活正常")
    : runtime.autostart_enabled
      ? (runtime.last_error ? `保活需要处理：${runtime.last_error}` : "自动启动已配置，等待健康记录")
      : "仅监听本机 · 尚未确认自动启动";
  document.getElementById("extension-version").textContent = formatVersion(manifestVersion);
  document.getElementById("knowledge-version").textContent = formatVersion(knowledge.version);
  document.getElementById("database-version").textContent = database.schema_version ? `Schema ${database.schema_version}` : "等待初始化";
  document.getElementById("database-status").textContent = database.status_label || "店铺数据仅保存在本机";
  document.getElementById("knowledge-expiry").textContent = knowledge.expires_at ? `有效期至 ${knowledge.expires_at.slice(0, 10)}` : "内置离线规则";
  document.getElementById("last-scan-at").textContent = scan.last_success_at || "暂无成功巡检";
  document.getElementById("stale-page-count").textContent = `${Number(scan.stale_page_count || 0)} 页`;
  document.getElementById("telemetry-opt-in").checked = Boolean(telemetry.enabled);
  const channel = document.getElementById("update-channel");
  if (system.channel) channel.value = system.channel;
  const readiness = document.getElementById("system-readiness");
  const productOperational = system.product_operational ?? system.ready !== false;
  const publicDistributionReady = system.public_distribution_ready === true;
  const healthy = productOperational && database.status !== "error" && knowledge.status !== "error" && versionCompatible;
  readiness.textContent = healthy ? "可离线判断" : "需要处理";
  readiness.className = healthy ? "ready" : versionCompatible ? "error" : "warn";
  document.getElementById("system-update-state").textContent = system.ai_required ? "需要 AI 服务" : "本地运行 · AI 可选";
  const releaseState = document.getElementById("release-readiness-state");
  if (publicDistributionReady) {
    releaseState.textContent = "可公开发行";
    releaseState.className = "ready";
  } else if (productOperational) {
    releaseState.textContent = "本地可用 · 发行受阻";
    releaseState.className = "blocked";
  } else {
    releaseState.textContent = "本地能力未就绪";
    releaseState.className = "blocked";
  }
  const blockers = Array.isArray(release.blockers) ? release.blockers : [];
  document.getElementById("release-readiness-summary").textContent = publicDistributionReady
    ? "本地能力与公开发行证据均已通过，可进入正式发布流程。"
    : productOperational
      ? `本地 Agent 可正常使用；公开推广仍有 ${blockers.length || 3} 项硬性条件未完成。`
      : "请先恢复本地数据库、知识包和版本一致性，再处理公开发行条件。";
  renderReleaseCheck(checks, "production_ed25519_trust", "release-ed25519-state", "release-ed25519-note", "生产信任锚已嵌入", "缺少生产 Ed25519 公钥");
  renderReleaseCheck(checks, "windows_authenticode", "release-authenticode-state", "release-authenticode-note", "完整发布链签名已确认", "Agent、更新器或安装升级入口签名未完整");
  renderReleaseCheck(checks, "browser_store_publication", "release-store-state", "release-store-note", "当前扩展商店来源与版本已确认", "商店发布、官方扩展 ID、来源或版本未全部核验");
  const sourceLabels = {
    unpacked: "开发者模式加载",
    release_bundle: "离线发布包",
    chrome_web_store: "Chrome 商店",
    edge_addons: "Edge 商店",
    "360_extension_store": "360 扩展商店",
  };
  document.getElementById("extension-install-source").textContent = sourceLabels[extensionDistribution.source] || "尚未上报";
  const queuedCount = Number(localQueue.queued_count || 0);
  document.getElementById("feedback-queue-state").textContent = telemetry.enabled ? `已同意 · 本地 ${queuedCount} 条` : `默认关闭 · 本地 ${queuedCount} 条`;
  document.getElementById("feedback-queue-note").textContent = localQueue.status === "error"
    ? `本地匿名反馈队列异常：${localQueue.error || "无法读取"}`
    : telemetry.enabled
      ? `仅保存已允许的粗粒度字段，本地排队 ${queuedCount} 条；当前不会自动上传。`
      : `未同意时不会入队；现有 ${queuedCount} 条仅保存在本机，可随时清空。`;
  const industry = knowledge.industry || "general";
  document.getElementById("industry-pack-state").textContent = `${industry} · ${formatVersion(knowledge.version)} · ${knowledge.source === "active" ? "已导入" : "内置"}`;
  const importButton = document.getElementById("import-industry-pack");
  importButton.disabled = !knowledge.local_import_supported || !knowledge.local_import_trust_configured;
  importButton.title = knowledge.local_import_trust_configured
    ? "导入经过 Ed25519 验签的行业知识包"
    : "尚未配置行业知识包验签公钥，已按安全策略禁用导入";
  const message = document.getElementById("update-message");
  message.className = `update-message ${update.error ? "error" : update.available ? "warn" : ""}`.trim();
  const updateText = update.error || update.message || "经营判断在本机完成；不连接 AI 也可正常诊断。";
  const offlineUpgradeText = system.offline_upgrade_production_available
    ? " 程序和扩展暂不支持在线升级，请使用已签名的生产离线升级包。"
    : " 离线签名机制已就绪，但生产信任锚尚未配置，生产离线升级暂不可用；development_test 包仅限开发测试。";
  message.textContent = !versionCompatible
    ? `版本不一致：本地 Agent ${formatVersion(system.agent_version)}，扩展 ${formatVersion(manifestVersion)}。请使用同一个升级包更新。`
    : `${updateText}${system.program_update_mode === "offline_bundle" && !update.available ? offlineUpgradeText : ""}`;
  document.getElementById("apply-knowledge-update").disabled = !update.knowledge_available;
  document.getElementById("rollback-knowledge").disabled = !knowledge.rollback_available;
}

async function loadSystemStatus() {
  try {
    const system = await bridgeFetch("/system/status");
    renderSystemStatus(system);
    return system;
  } catch (error) {
    renderSystemStatus({ ready: false, update: { error: error.message || "版本状态读取失败" } });
    return null;
  }
}

async function runUpdateAction(path, pendingText) {
  const message = document.getElementById("update-message");
  message.className = "update-message";
  message.textContent = pendingText;
  const channel = document.getElementById("update-channel").value;
  try {
    const result = await bridgeFetch(path, { method: "POST", body: JSON.stringify({ channel, component: "knowledge" }) });
    message.textContent = result.message || "操作已完成";
  } catch (error) {
    message.className = "update-message error";
    message.textContent = error.message || "操作失败，已保留当前可用版本";
  }
  await loadSystemStatus();
}

function renderOperatorMemory(memory = {}) {
  const status = document.getElementById("operator-memory-status");
  const note = document.getElementById("operator-memory-note");
  const list = document.getElementById("operator-memory-list");
  if (!status || !note || !list) return;
  const entries = Array.isArray(memory.entries) ? memory.entries : [];
  const scope = memory.scope || {};
  if (!scope.store_key) {
    status.textContent = "请先选择店铺";
    note.textContent = memory.note || "选择当前店铺并完成绑定后，经营记忆才会生效。";
    return empty(list, "尚未选择当前店铺，系统不会把不同店铺的经验混在一起。");
  }
  status.textContent = `${entries.length} 条可复用经验`;
  note.textContent = memory.note || "记忆只对当前店铺和千川账号生效。";
  if (!entries.length) return empty(list, "还没有经营记忆。可以先保存一条库存红线、投放策略或动作复盘。 ");
  list.className = "operator-memory-list";
  const labels = { fact: "事实", strategy: "策略", preference: "偏好", outcome: "结果" };
  const confidenceLabels = { low: "低置信", medium: "待验证", high: "高置信" };
  list.replaceChildren(...entries.slice(0, 12).map((item) => {
    const card = document.createElement("article");
    card.className = `operator-memory-item ${item.confidence || "medium"}`;
    const head = document.createElement("div"); head.className = "operator-memory-item-head";
    const title = document.createElement("strong"); title.textContent = item.title || "未命名记忆";
    const tag = document.createElement("span"); tag.textContent = `${labels[item.type] || "经验"} · ${confidenceLabels[item.confidence] || "待验证"}`;
    head.append(title, tag);
    const value = document.createElement("p"); value.textContent = item.value || "";
    const meta = document.createElement("small"); meta.textContent = item.updated_at ? `更新于 ${item.updated_at}` : "本地记忆";
    const archive = document.createElement("button"); archive.type = "button"; archive.className = "secondary"; archive.textContent = "归档";
    archive.addEventListener("click", async () => {
      archive.disabled = true;
      try {
        await bridgeFetch("/memory/archive", { method: "POST", body: JSON.stringify({ id: item.id }) });
        await loadOperatorMemory();
      } catch (error) {
        archive.disabled = false;
        meta.textContent = error.message || "归档失败";
      }
    });
    card.append(head, value, meta, archive);
    return card;
  }));
}

async function loadOperatorMemory() {
  try {
    const memory = await bridgeFetch("/memory");
    renderOperatorMemory(memory);
  } catch (error) {
    renderOperatorMemory({ note: error.message || "经营记忆暂时无法读取" });
  }
}

async function loadDashboard() {
  // Show loading skeleton
  showLoadingSkeleton();
  // Remember focus before re-render
  const focusedEl = document.activeElement;
  const focusId = focusedEl?.id || focusedEl?.closest("[id]")?.id;

  const [
    insightsR, actionCenterR, settingsR, opsR, extensionR, trendsR, accountsR, contextR, onboardingR, healthR, effectivenessR, readinessR, stopLossR, strategySimulationR, preflightR, shadowR, executionEffectivenessR, valueLedgerR, integrationsR, oceanengineR, oceanengineSyncR, connectionGuideR, promotionReadinessR
  ] = await Promise.allSettled([
    bridgeFetch("/insights"),
    bridgeFetch("/action-center"),
    bridgeFetch("/settings"),
    bridgeFetch("/ops-manager"),
    chrome.runtime.sendMessage({ type: "get-dashboard" }),
    bridgeFetch("/trends?days=7"),
    bridgeFetch("/qianchuan-accounts"),
    bridgeFetch("/operation-context"),
    bridgeFetch("/onboarding/status"),
    bridgeFetch("/health-monitor"),
    bridgeFetch("/effectiveness"),
    bridgeFetch("/actions/readiness"),
    bridgeFetch("/actions/stop-loss-queue"),
    bridgeFetch("/actions/strategy-simulation"),
    bridgeFetch("/actions/preflight"),
    bridgeFetch("/actions/shadow"),
    bridgeFetch("/actions/effectiveness"),
    bridgeFetch("/value-ledger"),
    bridgeFetch("/integrations"),
    bridgeFetch("/oauth/oceanengine/status"),
    bridgeFetch("/oauth/oceanengine/sync-status"),
    bridgeFetch("/connection-guide"),
    bridgeFetch("/qianchuan/promotion-readiness"),
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
  const onboarding = val(onboardingR, { status: "in_progress", progress: { completed: 1, total: 5 }, steps: [], current_step: { label: "环境检查", action: "none" } });
  const health = val(healthR, {});
  const effectiveness = val(effectivenessR, {});
  const readiness = val(readinessR, { items: [], summary: {}, criteria: [] });
  const stopLoss = val(stopLossR, { items: [], summary: {} });
  const strategySimulation = val(strategySimulationR, { scenarios: [] });
  const preflight = val(preflightR, { state: "idle", session: null, checks: [] });
  const shadow = val(shadowR, { items: [], summary: {} });
  const executionEffectiveness = val(executionEffectivenessR, { items: [], summary: {} });
  const valueLedger = val(valueLedgerR, { summary: {} });
  const integrations = val(integrationsR, { feishu: { configured: false }, dingtalk: { configured: false }, auto_send_reports: false });
  const oceanengine = val(oceanengineR, { app_id: "1871942906223351", connected: false, secret_saved: false, accounts: [] });
  const oceanengineSync = val(oceanengineSyncR, { synced_at: null });
  const connectionGuide = val(connectionGuideR, {
    level: "L0", level_label: "连接状态读取失败", collapsed: false, levels: [],
    next_upgrade: { id: "identify_store", label: "重新识别抖店", eta: "约 1 分钟", value: "恢复后继续经营诊断", failure: "连接向导暂时无法读取。现在请刷新后重新识别。" },
    tutorial: [], operation_context: operationContext, onboarding,
  });
  const promotionReadiness = val(promotionReadinessR, null);

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
  renderOperationContext(connectionGuide.operation_context || operationContext);
  renderOnboarding(connectionGuide.onboarding || onboarding);
  renderPlans(actionCenter.plan_recommendations || []);
  renderInventory(actionCenter.inventory_alerts || []);
  renderOperations(ops, actionCenter.shelf_analysis || {}, actionCenter.live_analysis || {}, actionCenter.creative_analysis || {}, insights.coverage || []);
  renderTodayFocus(ops);
  renderAlerts(insights.alerts || []);
  renderCoverage(insights.coverage || []);
  renderSettings(settings);
  renderIntegrations(integrations);
  renderOceanEngineOAuth(oceanengine);
  renderOceanEngineSync(oceanengineSync);
  renderQianchuanAccounts(accounts);
  renderConnectionGuide(connectionGuide, accounts);
  renderFullScan(extensionResponse?.dashboard?.fullScan || {});
  renderTrends(trends);
  renderHealthMonitor(health);
  renderEffectiveness(effectiveness);
  renderAutomationReadiness(readiness);
  renderStopLossQueue(stopLoss);
  renderStrategySimulation(strategySimulation);
  renderExecutionPreflight(preflight);
  renderShadowExecution(shadow);
  renderExecutionEffectiveness(executionEffectiveness);
  renderValueLedger(valueLedger);
  if (promotionReadiness) renderChengfangReadiness(promotionReadiness);
  else renderChengfangUnavailable(promotionReadinessR.reason?.message);
  await loadSystemStatus();
  await loadOperatorMemory();

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
      qianchuanFeatureDeferred = false;
      await chrome.storage.local.set({ qianchuanFeatureDeferred: false });
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
  const stored = await chrome.storage.local.get(["preferredRole", "workbenchScene", "templateChecks", "scanStorePreference", "scanAccountPreference", "lastQianchuanManualSync", "qianchuanFeatureDeferred"]);
  if (stored.preferredRole) currentRole = ROLE_MIGRATION[stored.preferredRole] || stored.preferredRole;
  if (!ROLE_WORKBENCH[currentRole]) currentRole = "货架商品";
  if (SCENE_WORKBENCH[stored.workbenchScene]) workbenchScene = stored.workbenchScene;
  selectedQianchuanAccount = String(stored.scanAccountPreference || "");
  selectedStoreKey = String(stored.scanStorePreference || "");
  qianchuanFeatureDeferred = Boolean(stored.qianchuanFeatureDeferred);
  if (stored.templateChecks && typeof stored.templateChecks === "object") {
    const todayScope = `${selectedStoreKey || "unscoped"}:${localDateKey()}:`;
    templateChecks = Object.fromEntries(Object.entries(stored.templateChecks).filter(([key]) => key.startsWith(todayScope)));
    await chrome.storage.local.set({ templateChecks });
  }
  document.querySelectorAll("#role-nav button").forEach((item) => item.classList.toggle("active", item.dataset.role === currentRole));
  await chrome.storage.local.set({ preferredRole: currentRole });
  restoreQianchuanSyncUi(stored.lastQianchuanManualSync || {});
  renderWorkbench();
  applyModuleVisibility();
  await reportExtensionInstallSource().catch(() => undefined);
  refreshAll(false);
});
document.getElementById("refresh-button").addEventListener("click", () => refreshAll(false));
document.getElementById("sync-diagnose").addEventListener("click", () => refreshAll(true));
document.getElementById("chengfang-sync").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在同步…";
  try {
    await syncRecentQianchuanPage();
  } catch (_) {
    // The shared sync dock already presents the actionable error.
  } finally {
    button.disabled = false;
    button.textContent = "同步当前千川页";
  }
});
document.querySelectorAll("[data-promotion-view]").forEach((button) => button.addEventListener("click", () => {
  currentPromotionView = button.dataset.promotionView || "overview";
  document.querySelectorAll("[data-promotion-view]").forEach((item) => item.classList.toggle("active", item === button));
  document.getElementById("chengfang-panel").hidden = currentPromotionView !== "chengfang";
}));
document.getElementById("operator-memory-refresh").addEventListener("click", () => loadOperatorMemory());
document.getElementById("check-updates").addEventListener("click", () => runUpdateAction("/updates/check", "正在检查 Agent、扩展与知识包版本…"));
document.getElementById("apply-knowledge-update").addEventListener("click", () => runUpdateAction("/updates/apply", "正在验证并切换新的知识包…"));
document.getElementById("rollback-knowledge").addEventListener("click", () => runUpdateAction("/updates/rollback", "正在恢复上一个已验证的知识包…"));
document.getElementById("import-industry-pack").addEventListener("click", () => document.getElementById("industry-pack-file").click());
document.getElementById("industry-pack-file").addEventListener("change", async (event) => {
  const file = event.currentTarget.files?.[0];
  if (!file) return;
  const message = document.getElementById("update-message");
  message.className = "update-message";
  message.textContent = "正在验签并导入行业知识包…";
  try {
    const pack = JSON.parse(await file.text());
    const result = await bridgeFetch("/rules/import-local", { method: "POST", body: JSON.stringify({ pack }) });
    message.textContent = result.message || "行业知识包已导入并启用";
  } catch (error) {
    message.className = "update-message error";
    message.textContent = error.message || "行业知识包导入失败，当前版本未改变";
  } finally {
    event.currentTarget.value = "";
    await loadSystemStatus();
  }
});
document.getElementById("clear-feedback-queue").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const result = await bridgeFetch("/telemetry/queue/clear", { method: "POST", body: JSON.stringify({ confirm: true }) });
    document.getElementById("feedback-queue-note").textContent = `已清空 ${Number(result.removed || 0)} 条本地匿名反馈；店铺数据未受影响。`;
    await loadSystemStatus();
  } catch (error) {
    document.getElementById("feedback-queue-note").textContent = error.message || "本地匿名反馈队列清空失败";
  } finally {
    button.disabled = false;
  }
});
document.getElementById("update-channel").addEventListener("change", async (event) => {
  await bridgeFetch("/updates/channel", { method: "POST", body: JSON.stringify({ channel: event.currentTarget.value }) });
  await loadSystemStatus();
});
document.getElementById("telemetry-opt-in").addEventListener("change", async (event) => {
  const enabled = event.currentTarget.checked;
  try {
    await bridgeFetch("/telemetry/settings", { method: "POST", body: JSON.stringify({ enabled }) });
    await loadSystemStatus();
  } catch (error) {
    event.currentTarget.checked = !enabled;
    const message = document.getElementById("update-message");
    message.className = "update-message error";
    message.textContent = error.message || "匿名改进计划设置失败";
  }
});
document.getElementById("operator-memory-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    await bridgeFetch("/memory/upsert", {
      method: "POST",
      body: JSON.stringify({
        title: document.getElementById("operator-memory-title").value.trim(),
        type: document.getElementById("operator-memory-type").value,
        value: document.getElementById("operator-memory-value").value.trim(),
        source: "user",
        confidence: "medium",
      }),
    });
    form.reset();
    await loadOperatorMemory();
  } catch (error) {
    document.getElementById("operator-memory-note").textContent = error.message || "保存记忆失败";
  } finally {
    button.disabled = false;
  }
});
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
document.getElementById("connection-guide-action").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const action = button.dataset.action || currentConnectionGuide?.next_upgrade?.id || "none";
  if (action === "identify_store") {
    await chrome.tabs.create({ url: "https://fxg.jinritemai.com/ffa/mshop/homepage/index", active: true });
    document.getElementById("connection-failure-help").textContent = "抖店经营概览已打开。页面加载完成后回到这里点击重新识别；我们不会读取账号密码。";
    button.dataset.action = "retry_identify_store";
    button.textContent = "重新识别当前抖店";
    return;
  }
  if (action === "retry_identify_store") {
    button.disabled = true;
    button.textContent = "正在重新识别…";
    try {
      await chrome.runtime.sendMessage({ type: "manual-sync" });
      await new Promise((resolve) => setTimeout(resolve, 600));
      await loadDashboard();
    } finally {
      button.disabled = false;
    }
    return;
  }
  if (action === "confirm_store" || action === "select_store") {
    const select = document.getElementById("qianchuan-account-select");
    const candidates = [...select.options].filter((option) => option.value);
    const candidate = selectedStoreKey || (candidates.length === 1 ? candidates[0].value : "");
    if (candidate) {
      button.disabled = true;
      try {
        await bridgeFetch("/stores/select", { method: "POST", body: JSON.stringify({ store_key: candidate }) });
        await loadDashboard();
      } finally {
        button.disabled = false;
      }
      return;
    }
    document.getElementById("connection-store-controls").hidden = false;
    select.focus();
    select.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  if (action === "quick_scan") {
    if (!selectedStoreKey) {
      document.getElementById("qianchuan-account-select").focus();
      return;
    }
    button.disabled = true;
    button.textContent = "正在同步关键页面…";
    try {
      await chrome.runtime.sendMessage({
        type: "start-full-scan",
        scan_scope: "quick",
        store_key: selectedStoreKey,
        account_key: selectedQianchuanAccount,
        page_ids: selectedQianchuanAccount
          ? ["overview", "orders", "products", "shelf", "qianchuan_overview", "qianchuan_campaigns"]
          : ["overview", "orders", "products", "shelf"],
      });
      document.querySelector(".scan-card")?.scrollIntoView({ behavior: "smooth", block: "start" });
      await loadDashboard();
    } finally {
      button.disabled = false;
    }
    return;
  }
  if (action === "sync_qianchuan") {
    await syncRecentQianchuanPage().catch(() => undefined);
    return;
  }
  if (action === "view_ad_candidates") {
    document.getElementById("automation-candidates")?.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  if (action === "view_controlled_execution") {
    document.getElementById("execution-preflight")?.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  await bridgeFetch("/onboarding/update", {
    method: "POST",
    body: JSON.stringify({ event: "first_task_viewed" }),
  }).catch(() => undefined);
  document.getElementById("manager-tasks")?.scrollIntoView({ behavior: "smooth", block: "center" });
  await loadDashboard();
});
document.getElementById("priority-reminder-action").addEventListener("click", (event) => {
  const mode = event.currentTarget.dataset.mode;
  if (mode === "checking") return;
  if (mode === "data") {
    document.getElementById("connection-guide-action").click();
    return;
  }
  if (mode === "review") {
    const receipt = document.getElementById("scan-receipt-card");
    if (receipt) receipt.open = true;
    receipt?.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  document.getElementById("manager-tasks")?.scrollIntoView({ behavior: "smooth", block: "center" });
});
document.getElementById("manager-expand").addEventListener("click", () => {
  managerQueueExpanded = !managerQueueExpanded;
  if (!currentOperationsContext) return;
  const { ops, shelf, live, creative, coverage } = currentOperationsContext;
  renderOperations(ops, shelf, live, creative, coverage);
});
document.getElementById("next-best-action-button").addEventListener("click", (event) => {
  const target = document.getElementById(event.currentTarget.dataset.target || "manager-tasks");
  target?.scrollIntoView({ behavior: "smooth", block: "center" });
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
  const operationType = button.dataset.operationType || "adjust_budget";
  const confirmationText = operationType === "pause_plan"
    ? "确认暂停该计划"
    : `确认${operationType === "restore_budget" ? "恢复预算" : "降低预算"}至${button.dataset.targetValue || ""}`;
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
      if (!execution?.ok) throw new Error(execution?.error || "预算受控执行失败");
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
  if (!selectedStoreKey) {
    const select = document.getElementById("qianchuan-account-select");
    select.focus();
    document.getElementById("scan-detail").textContent = "请先选择当前店铺，避免把多个千川账号的数据混在一起。";
    return;
  }
  await chrome.runtime.sendMessage({
    type: "start-full-scan",
    scan_scope: "full",
    store_key: selectedStoreKey,
    account_key: selectedQianchuanAccount,
    page_ids: selectedQianchuanAccount ? null : DOUDIAN_SCAN_PAGE_IDS,
  });
  await loadDashboard();
});
document.getElementById("qianchuan-account-select").addEventListener("change", async (event) => {
  selectedStoreKey = event.currentTarget.value;
  accountSelectionRequired = false;
  await chrome.storage.local.set({ scanStorePreference: selectedStoreKey });
  await bridgeFetch("/stores/select", { method: "POST", headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" }, body: JSON.stringify({ store_key: selectedStoreKey }) });
  await loadDashboard();
});
document.getElementById("connection-switch-store").addEventListener("click", () => {
  document.getElementById("connection-guide").className = "connection-guide expanded";
  document.getElementById("connection-status-strip").hidden = true;
  document.getElementById("connection-guide-expanded").hidden = false;
  document.getElementById("connection-store-controls").hidden = false;
  document.getElementById("qianchuan-account-select").focus();
});
document.getElementById("connection-skip-qianchuan").addEventListener("click", async () => {
  qianchuanFeatureDeferred = true;
  await chrome.storage.local.set({ qianchuanFeatureDeferred: true });
  document.getElementById("connection-skip-qianchuan").textContent = "已暂不使用，可随时再连接";
  renderConnectionGuide(currentConnectionGuide || {});
});
document.getElementById("link-account-button").addEventListener("click", async () => {
  const accountKey = document.getElementById("unlinked-account-select").value;
  if (!selectedStoreKey || !accountKey) return;
  await bridgeFetch("/stores/link", { method: "POST", headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" }, body: JSON.stringify({ store_key: selectedStoreKey, account_key: accountKey }) });
  await loadDashboard();
});
document.getElementById("current-qianchuan-button").addEventListener("click", () => {
  syncRecentQianchuanPage().catch(() => undefined);
});
document.getElementById("qianchuan-sync-dock-button").addEventListener("click", () => {
  syncRecentQianchuanPage().catch(() => undefined);
});
document.getElementById("automation-sync-qianchuan").addEventListener("click", () => {
  syncRecentQianchuanPage().catch(() => undefined);
});
document.getElementById("automation-skip").addEventListener("click", async (event) => {
  qianchuanFeatureDeferred = true;
  await chrome.storage.local.set({ qianchuanFeatureDeferred: true });
  event.currentTarget.textContent = "已暂不使用，可随时再连接";
  event.currentTarget.disabled = true;
  const offState = document.getElementById("automation-off-state");
  offState.querySelector("strong").textContent = "已暂不使用投放功能";
  offState.querySelector("p").textContent = "抖店巡店与经营诊断会继续正常使用；需要投放时，再同步当前千川页即可开启。";
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
    status.textContent = "设置已保存，建议已按新阈值刷新。";
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
