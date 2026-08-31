const BRIDGE_URL = "http://127.0.0.1:8765";
const bridgeAuthClient = globalThis.DianBridgeAuth.createClient({ baseUrl: BRIDGE_URL });
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
let scanPollInFlight = false;
let fullScanRunning = false;
let workbenchScene = "daily";
let templateChecks = {};
let managerQueueExpanded = false;
let currentPreflightSession = null;
let currentPreflightState = "idle";
let currentPreflightAction = null;
const qianchuanSyncPromises = new Map();
let qianchuanSyncGeneration = 0;
let qianchuanSyncActiveCount = 0;
let latestQianchuanSyncUiReceipt = { success: null, attempt: null };
let qianchuanSyncDockCompactPreferenceSet = false;
let qianchuanSyncFreshnessTimer = null;
let oceanengineStatusPoller = null;
let oceanengineStatusPollInFlight = false;
let oceanengineStatusPollEpoch = 0;
let oceanengineStatusRefreshGeneration = 0;
let currentOceanEngineAccountCenter = null;
let currentOceanEngineBatchPreview = null;
let oceanEngineAccountSelection = new Set();
let currentOperationContext = null;
let currentOnboarding = null;
let currentConnectionGuide = null;
let qianchuanFeatureDeferred = false;
let currentPromotionView = "chengfang";
let currentChengfangLocalPlan = { schema_version: 5, goal: "", inputs: {}, boundaries: {}, evidence: {}, shadow: { enabled: false }, scope: {}, scope_key: "" };
let currentChengfangLocalPlanStore = { schema_version: 5, plans_by_scope: {} };
let currentChengfangGate = { identity_ready: false, metric_ready: false, data_ready: false, next_step: "先同步真实乘方页面。" };
let currentChengfangAgentRuntime = null;
let currentChengfangA2Pilot = null;
let currentChengfangDemoFixture = null;
let currentExtensionSettings = { autoSync: false, intervalMinutes: 5 };
let currentChengfangTrialConfig = { schema_version: 2, environment: "demo", plan_whitelist: [], budget_cap: null };
let currentChengfangCandidatePath = null;
let currentChengfangProductionWrite = { status: {}, targets: [], active: null, operation: null, kill_switch: {}, unsafe_target_response: false };
let currentQianchuanCatalog = null;
let currentKnowledgeCatalog = null;
let currentAutopilotCenterSettings = { schema_version: 1, mode: "protect", max_batch: 3 };
let currentAutopilotCenterView = null;
let currentAutopilotPackagePreview = null;
let currentAutopilotPackages = {};
let currentAutopilotActivity = [];
let currentControlTaskSummary = null;
let currentControlTaskFamily = "budget";
let currentControlTaskView = null;
let currentScheduleControl = null;
let currentScheduleView = null;
let scheduleDraftEnabled = false;
let scheduleDraftRanges = [{ start: "09:00", end: "12:00" }, { start: "18:00", end: "22:00" }];
let scheduleDraftHydrated = false;
let currentCreativeAnalysis = {};
let currentMaterialGovernancePreview = null;
let currentMaterialGovernancePackages = {};
let currentPromotionPlanConsole = { rows: [], accounts: [], safe: true };
let currentPromotionPlanView = null;
let currentPromotionPlanFilters = { account: "all", mode: "all", plan_type: "all", status: "all", binding: "all", query: "", features: [] };
let currentPromotionPlanRecovery = null;
let currentPromotionPlanBindings = {};
let selectedPromotionPlanKeys = new Set();
let currentPromotionBulkActionDraft = null;
let currentPromotionBulkActionDrafts = [];
let allPromotionBulkActionDrafts = [];
let currentPromotionOperationAudit = { actions: [], execution_enabled: false, summary: {} };
let currentPromotionOperationLogView = null;
let currentPromotionOperationLogFilters = { operation_type: "all", state: "all", query: "", date_from: "", date_to: "" };
let currentPromotionBatchDraft = null;
let currentPromotionBatchDrafts = [];
let currentBatchPlanMode = "chengfang";
let experienceMode = "simple";
let currentSimpleJourney = null;
let currentWorkspaceTarget = "today-task-center";
let currentJourneyLane = "";
let currentJourneyInputs = null;
let currentJourneyCommand = null;
let currentDashboardFailures = [];
let agentConnectionState = "checking";
const DEFAULT_AGENT_WRITE_BLOCK_MESSAGE = "本地 Agent 未连接，写入与执行已冻结；请先修复本地 Agent。";
const CHECKING_AGENT_WRITE_BLOCK_MESSAGE = "正在核验本地 Agent、扩展版本和会话；完成前写入与执行保持冻结。";
const RECOVERABLE_AGENT_AUTH_ERROR_CODES = new Set([
  "agent_auth_timeout", "agent_session_timeout", "agent_session_required",
  "agent_session_invalid", "agent_session_expired",
]);
let agentWriteBlockMessage = DEFAULT_AGENT_WRITE_BLOCK_MESSAGE;
let dashboardLoadGeneration = 0;
const DASHBOARD_READ_RETRY_DELAY_MS = 250;
const DASHBOARD_READ_TIMEOUT_MS = 5000;
const DASHBOARD_READ_CONCURRENCY = 4;
const AGENT_AUTO_RECOVERY_COOLDOWN_MS = 5000;
let dashboardLoadInFlight = null;
let dashboardLoadTrailing = null;
let agentAutoRecoveryInFlight = null;
let agentAutoRecoveryLastAttemptAt = 0;
let latestFullScanSnapshot = {};
let chengfangProfileSyncTimer = null;
let chengfangShadowSetupReady = false;
let currentAiStatus = { configured: false, analysis_available: false, shadow_running: false };
let currentAiProposals = [];
const CHENGFANG_TRIAL_CONFIG_KEY = "chengfangTrialConsoleV1";
const AUTOPILOT_CENTER_SETTINGS_KEY = "autopilotCenterV1";
const AUTOPILOT_PACKAGES_KEY = "autopilotPackagesV1";
const AUTOPILOT_ACTIVITY_KEY = "autopilotActivityV1";
const MATERIAL_GOVERNANCE_PACKAGES_KEY = "materialGovernancePackagesV1";
const PROMOTION_PLAN_FILTERS_KEY = "promotionPlanFiltersV1";
const PROMOTION_PLAN_BINDINGS_KEY = "promotionPlanBindingsV1";
const PROMOTION_BULK_ACTION_DRAFTS_KEY = "promotionBulkActionDraftsV1";
const PROMOTION_BATCH_DRAFTS_KEY = "promotionBatchDraftsV1";
const EXPERIENCE_MODE_KEY = "experienceModeV2";
const JOURNEY_LANE_KEY = "journeyLaneV1";
const QIANCHUAN_SYNC_DOCK_HIDDEN_KEY = "qianchuanSyncDockHiddenV1";
const QIANCHUAN_SYNC_DOCK_COMPACT_KEY = "qianchuanSyncDockCompactV1";
const QIANCHUAN_SYNC_ATTEMPT_KEY = "lastQianchuanManualSyncAttempt";
const PAGE_SYNC_RECEIPTS_KEY = "pageSyncReceiptsV1";
const QIANCHUAN_SYNC_FRESH_MS = 30 * 60 * 1000;
const CHENGFANG_DEMO_ENTRY_ROUTE = "chengfang-demo";
const SCAN_TIMEOUT_MS = 5 * 60 * 1000; // 5 minutes
const QUICK_SCAN_PAGE_IDS = Object.freeze(DianAgentScanScopePolicy.pageIds("core_doudian"));
const AGENT_WRITE_CONTROL_IDS = Object.freeze([
  "link-account-button", "select-linked-account-button", "unlink-account-button", "apply-knowledge-update", "rollback-knowledge", "import-industry-pack", "activate-industry-pack", "clear-feedback-queue",
  "ai-test-connection", "ai-save-connection", "ai-disable-connections", "ai-run-shadow",
  "authorize-oceanengine", "oauth-center-run-sync", "save-integration-settings", "autopilot-package-apply",
  "control-task-builder-create", "control-task-primary", "schedule-control-save", "chengfang-profile-reset",
  "chengfang-evidence-hydrate", "chengfang-shadow-toggle", "chengfang-trial-sync-toggle", "chengfang-trial-whitelist-add",
  "chengfang-trial-start", "chengfang-trial-review-accept", "chengfang-trial-review-reject", "chengfang-trial-execute",
  "chengfang-trial-readback", "chengfang-trial-stop", "chengfang-evaluate-now", "chengfang-emergency-stop",
  "chengfang-production-prepare", "chengfang-production-authorize", "chengfang-production-execute",
  "chengfang-production-cancel", "chengfang-production-reconcile", "chengfang-production-stop", "chengfang-production-resume",
  "material-governance-save", "preflight-authorize", "preflight-archive", "preflight-stop", "batch-plan-generate", "save-settings",
  "save-report-settings", "generate-report", "generate-send-report", "qianchuan-account-select",
]);
const AGENT_WRITE_DYNAMIC_SELECTOR = [
  ".confirm-action-btn", "[data-control-task-action]", "[data-schedule-action]", "[data-chengfang-feedback]",
  "[data-promotion-plan-action]", "[data-task-write]", "[data-integration-test]", "[data-integration-clear]",
  "[data-agent-write]",
].join(",");

function requestedWorkbenchEntryRoute() {
  const hashRoute = String(globalThis.location?.hash || "").replace(/^#/, "").trim().toLowerCase();
  const queryRoute = new URLSearchParams(String(globalThis.location?.search || "")).get("entry")?.trim().toLowerCase() || "";
  return [CHENGFANG_DEMO_ENTRY_ROUTE, "a2-demo"].includes(queryRoute || hashRoute)
    ? CHENGFANG_DEMO_ENTRY_ROUTE
    : "";
}

function setPromotionView(view = "overview") {
  const route = globalThis.DianPromotionPlanCenter.promotionViewRoute(view);
  currentPromotionView = route.view;
  document.querySelectorAll("[data-promotion-view]").forEach((item) => {
    const active = item.dataset.promotionView === currentPromotionView;
    item.classList.toggle("active", active);
    item.setAttribute("aria-pressed", String(active));
  });
  document.getElementById("chengfang-panel").hidden = currentPromotionView !== "chengfang";
  return route;
}

function navigatePromotionView(view = "overview") {
  const route = setPromotionView(view);
  const copy = {
    overview: ["全部投放计划", "统一筛选和管理直播、商品、全域、乘方与随心推计划。"],
    standard: ["标准计划", "只查看标准计划；账户、状态和本地托管筛选继续保留。"],
    full_domain: ["全域推广", "只查看全域推广计划；账户、状态和本地托管筛选继续保留。"],
    chengfang: ["千川乘方", "进入乘方只读诊断、影子评估和本机模拟工作台。"],
  }[route.view];
  if (route.filters) {
    currentPromotionPlanFilters = globalThis.DianPromotionPlanCenter.normalizeFilters({
      ...currentPromotionPlanFilters,
      ...route.filters,
    });
    renderPromotionPlanConsole();
    persistPromotionPlanFilters().catch(() => undefined);
  }
  return navigateToWorkspaceElement(route.target_id, {
    role: "直播投放",
    workspaceKey: route.target_id === "promotion-plan-center" ? "promotion-overview" : "",
    title: copy[0],
    subtitle: copy[1],
  });
}

function focusWorkbenchEntryRoute(route) {
  if (route !== CHENGFANG_DEMO_ENTRY_ROUTE) {
    const entryTarget = globalThis.DianSimpleExperience.onboardingEntryTarget({
      mode: experienceMode,
      route,
      journey: currentSimpleJourney || {},
    });
    if (entryTarget === "simple-start") {
      const onboardingCard = document.getElementById("simple-start");
      currentWorkspaceTarget = "simple-start";
      document.getElementById("workspace-current-title").textContent = "开始使用";
      document.getElementById("workspace-page-title").textContent = "完成当前唯一一步";
      document.getElementById("workspace-page-subtitle").textContent = "先连接店铺并取得新鲜经营数据；完成后才进入今日任务。";
      clearWorkspacePageFocus();
      focusWorkspacePage(onboardingCard);
      configureWorkspacePrimaryAction("simple-start");
      onboardingCard?.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    const todayRoute = document.querySelector('#role-nav [data-workspace-key="today-tasks"]');
    if (todayRoute) navigateWorkspaceTarget(todayRoute);
    return;
  }
  currentRole = "直播投放";
  document.querySelectorAll("#role-nav button").forEach((item) => item.classList.toggle("active", item.dataset.role === currentRole));
  renderWorkbench();
  applyModuleVisibility();
  setPromotionView("chengfang");
  const section = document.getElementById("chengfang-one-click-demo");
  const button = document.getElementById("chengfang-one-click-demo-run");
  currentWorkspaceTarget = "chengfang-one-click-demo";
  clearWorkspacePageFocus();
  focusWorkspacePage(section);
  section?.classList.add("activation-entry-focus");
  section?.scrollIntoView({ behavior: "smooth", block: "center" });
  button?.focus({ preventScroll: true });
  globalThis.setTimeout?.(() => section?.classList.remove("activation-entry-focus"), 3200);
}

const ROLE_WORKBENCH = {
  "货架商品": {
    nav_label: "商品经营",
    title: "商品经营",
    description: "一件商品一张卡，只看状态、核心指标、风险和下一步",
    tasks: [
      ["product_cards", "检查商品卡", "重点商品的状态、库存、成交和风险已经核对"],
      ["product_risk", "处理商品风险", "每件异常商品只保留一个明确下一步"],
    ],
  },
  "直播投放": {
    nav_label: "直播与投放",
    title: "直播与投放",
    description: "统一管理直播数据、全部投放计划、自动投放策略、执行准备和结果回读",
    tasks: [
      ["live_funnel", "核对直播与投放漏斗", "进房、商品点击、成交、消耗和 ROI 瓶颈已定位"],
      ["ad_risk", "处理高消耗低转化计划", "每项调整都有依据、幅度、授权和观察窗口"],
      ["live_review", "复盘直播投放结果", "异常时段已关联到计划调整和下一次复查任务"],
    ],
  },
  "内容": {
    nav_label: "内容素材",
    title: "内容素材",
    description: "只展示素材及其核心表现，不混入投放设置和经营策略",
    tasks: [
      ["material_performance", "检查素材表现", "高风险、观察中和可复用素材已经看清"],
      ["material_inventory", "补齐素材数据", "重点素材都有消耗、ROI 和成交证据"],
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

const ROLE_DEFAULT_TARGET = Object.freeze({
  "货架商品": "product-operating-graph",
  "直播投放": "live-plan-management-section",
  "内容": "content-workbench",
});

const WORKSPACE_PATH_ORDER = Object.freeze(["prepare", "diagnose", "act", "verify"]);

function workspacePathStage(targetId = "") {
  if (["connection-guide", "oceanengine-oauth-card", "scan-card", "data-version-center", "industry-pack-center", "ai-connection-center", "strategy-settings-card"].includes(targetId)) return "prepare";
  if (["promotion-bulk-action-center", "batch-plan-preparation", "promotion-mode-workbench", "autopilot-center", "control-task-center", "automation-policy-center", "schedule-control-studio", "automation-section"].includes(targetId)) return "act";
  if (["promotion-operation-log", "report-center-card", "value-ledger-card"].includes(targetId)) return "verify";
  return "diagnose";
}

function workspacePathCopy(stage, targetId, contextTitle = "") {
  const roleTarget = ROLE_DEFAULT_TARGET[currentRole] || "today-task-center";
  if (stage === "prepare") {
    const scanning = targetId === "scan-card";
    return {
      title: scanning ? "正在准备经营数据" : "先打开抖店，再选择投放账户",
      detail: scanning ? "巡店完成后只把异常和缺失页交给你处理。" : "系统会自动准备抖店数据；需要投放时再选择千川账户。",
      next_target: scanning ? "today-task-center" : "scan-card",
      next_label: scanning ? "下一步：查看今日任务" : "下一步：开始巡店",
      next_role: "",
    };
  }
  if (stage === "act") {
    return {
      title: `${contextTitle || "当前任务"}：一次只处理一个变量`,
      detail: "先看依据和影响，再人工确认；阻塞项不会进入下一步。",
      next_target: "promotion-operation-log",
      next_label: "下一步：查看执行与回读",
      next_role: "直播投放",
    };
  }
  if (stage === "verify") {
    return {
      title: "用结果回读判断调整是否有效",
      detail: "没有回读证据的动作不会被标记为成功，也不会沉淀为经营规律。",
      next_target: "today-task-center",
      next_label: "回到今日任务",
      next_role: "",
    };
  }
  const onTaskCenter = ["today-task-center", "next-best-action", "priority-reminder"].includes(targetId);
  return {
    title: onTaskCenter ? "今天先处理最重要的一件事" : `${contextTitle || "当前页面"}：只看异常和判断依据`,
    detail: "系统按风险、收益和数据可信度排序，正常项默认不占用注意力。",
    next_target: onTaskCenter ? roleTarget : "next-best-action",
    next_label: onTaskCenter ? `进入${ROLE_WORKBENCH[currentRole]?.title || "经营页面"}` : "查看优先动作",
    next_role: onTaskCenter ? currentRole : "",
  };
}

function renderWorkspaceUserPath(targetId = ROLE_DEFAULT_TARGET[currentRole], contextTitle = "") {
  const stage = workspacePathStage(targetId);
  const stageIndex = WORKSPACE_PATH_ORDER.indexOf(stage);
  const copy = workspacePathCopy(stage, targetId, contextTitle);
  document.querySelectorAll("#workspace-user-path-steps [data-workspace-path-stage]").forEach((item) => {
    const index = WORKSPACE_PATH_ORDER.indexOf(item.dataset.workspacePathStage);
    item.classList.toggle("complete", index < stageIndex);
    item.classList.toggle("current", index === stageIndex);
  });
  document.getElementById("workspace-user-path-title").textContent = copy.title;
  document.getElementById("workspace-user-path-detail").textContent = copy.detail;
  const button = document.getElementById("workspace-user-path-next");
  button.textContent = copy.next_label;
  button.dataset.workspaceTarget = copy.next_target;
  const nextRoute = [...document.querySelectorAll("#role-nav button[data-workspace-target]")]
    .find((item) => item.dataset.workspaceTarget === copy.next_target);
  button.dataset.workspaceTitle = nextRoute?.dataset.workspaceTitle || (copy.next_target === "next-best-action" ? "优先动作" : copy.next_label.replace(/^下一步：/, ""));
  button.dataset.workspaceSubtitle = nextRoute?.dataset.workspaceSubtitle || "先看系统给出的依据，再决定是否处理。";
  const nextRole = copy.next_role || nextRoute?.dataset.workspaceRole || "";
  if (nextRole) button.dataset.workspaceRole = nextRole;
  else delete button.dataset.workspaceRole;
}

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

function templateCheckDateFromKey(key = "") {
  const parts = String(key || "").split(":");
  return parts.length >= 5 ? parts[1] : "";
}

function pruneTemplateChecksForDate(checks = {}, dateKey = localDateKey()) {
  const source = checks && typeof checks === "object" && !Array.isArray(checks) ? checks : {};
  return Object.fromEntries(Object.entries(source).filter(([key]) => templateCheckDateFromKey(key) === dateKey));
}

function templateCheckKey(taskId) {
  return `${selectedStoreKey || "unscoped"}:${localDateKey()}:${workbenchScene}:${currentRole}:${taskId}`;
}

function renderWorkbench() {
  const role = ROLE_WORKBENCH[currentRole] || ROLE_WORKBENCH["货架商品"];
  const scene = SCENE_WORKBENCH[workbenchScene] || SCENE_WORKBENCH.daily;
  document.getElementById("workbench-title").textContent = role.title;
  document.getElementById("workbench-description").textContent = role.description;
  document.getElementById("workspace-current-title").textContent = role.nav_label || currentRole;
  document.getElementById("workspace-page-title").textContent = role.title;
  document.getElementById("workspace-page-subtitle").textContent = `${role.description}。当前班次：${scene.label}。`;
  document.getElementById("workbench-scene").value = workbenchScene;
  document.getElementById("template-heading").textContent = `${scene.label} · 标准动作`;
  document.getElementById("template-intro").textContent = scene.intro;
  renderWorkspaceUserPath(ROLE_DEFAULT_TARGET[currentRole], role.title);
  configureWorkspacePrimaryAction(ROLE_DEFAULT_TARGET[currentRole]);

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
      if (templateChecks[key]) delete templateChecks[key];
      else templateChecks[key] = true;
      // Keys are scoped as <store>:<date>:<scene>:<role>:<task>. Keep today's
      // records for every store so switching stores cannot erase another
      // store's checklist, while old daily records do not grow forever.
      templateChecks = pruneTemplateChecksForDate(templateChecks);
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
    markAgentWriteControl(confirmButton);
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
        if (!isConfirmedNow && params.state === "confirmed") {
          await Promise.allSettled([refreshAutomationReadiness(), refreshShadowExecution()]);
          navigateToWorkspaceElement("automation-section", {
            role: "直播投放",
            workspaceKey: "controlled-execution",
            focusElement: document.getElementById("automation-status"),
          });
        } else {
          refreshAutomationReadiness().catch(() => undefined);
          refreshShadowExecution().catch(() => undefined);
        }
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
  if (scanPollInFlight) return;
  scanPollInFlight = true;
  try {
    const response = await chrome.runtime.sendMessage({ type: "get-dashboard" });
    if (!response?.ok) return;
    const scan = response.dashboard?.fullScan || {};
    const wasRunning = fullScanRunning;
    if (!renderFullScan(scan)) return;
    if (wasRunning && ["completed", "partial", "cancelled", "interrupted", "error"].includes(scan.status)) {
      await loadDashboard();
    }
  } finally {
    scanPollInFlight = false;
  }
}

function normalizeCoreRefreshPageIds(value = [], { fallback = true } = {}) {
  const allowed = new Set(QUICK_SCAN_PAGE_IDS);
  const requested = Array.isArray(value) ? value : [];
  const pageIds = [...new Set(requested.map((item) => String(item || "").trim()).filter((item) => allowed.has(item)))];
  return pageIds.length || !fallback ? pageIds : [...QUICK_SCAN_PAGE_IDS];
}

const DOUDIAN_OVERVIEW_URL = "https://fxg.jinritemai.com/ffa/mshop/homepage/index";

function isDoudianTab(tab = {}) {
  return String(tab?.url || "").startsWith("https://fxg.jinritemai.com/");
}

async function waitForDoudianOverview(tabId, timeoutMs = 18000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const tab = await chrome.tabs.get(tabId).catch(() => null);
    if (!tab) throw new Error("抖店页面已经关闭，请重新开始。");
    if (tab.status === "complete" && isDoudianTab(tab)) {
      await new Promise((resolve) => setTimeout(resolve, 900));
      return tab;
    }
    await new Promise((resolve) => setTimeout(resolve, 350));
  }
  throw new Error("抖店页面加载较慢，请等待页面打开后再点“开始巡店”。");
}

function simpleStorePreparationError(error = {}) {
  const code = String(error?.code || error?.error_code || "").toUpperCase();
  const message = String(error?.message || error || "");
  if (code === "LOGIN_REQUIRED" || /登录/.test(message)) {
    return "请先完成抖店登录，登录后再点“开始巡店”。";
  }
  if (["STORE_IDENTITY_CONFLICT", "STORE_MISMATCH", "STORE_SELECTION_CONFLICT"].includes(code)) {
    return "检测到店铺正在切换，本次没有继续。请回到要经营的抖店首页后重试。";
  }
  if (["STORE_IDENTITY_UNRESOLVED", "STORE_SELECTION_UNCONFIRMED", "SYNC_REJECTED"].includes(code)) {
    return "这个页面暂时不能用于经营判断。请打开抖店首页并刷新后重试。";
  }
  return message || "暂时无法读取当前抖店，请刷新页面后重试。";
}

async function ensureCurrentStoreForScan(triggerButton = null) {
  const [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true });
  let targetTab = isDoudianTab(activeTab) ? activeTab : null;

  // A previously confirmed scope can continue when the user is viewing the
  // side panel from another site. When no scope exists, bring the exact page
  // that will be accepted into the foreground instead of guessing from the
  // historical local catalog.
  if (!targetTab && selectedStoreKey) return selectedStoreKey;
  if (!targetTab) {
    const openTabs = await chrome.tabs.query({ url: "https://fxg.jinritemai.com/*", currentWindow: true });
    targetTab = openTabs.find((tab) => String(tab.url || "").includes("/ffa/mshop/homepage/")) || openTabs[0] || null;
    if (targetTab?.id) {
      targetTab = await chrome.tabs.update(targetTab.id, { active: true, url: DOUDIAN_OVERVIEW_URL });
    } else {
      targetTab = await chrome.tabs.create({ url: DOUDIAN_OVERVIEW_URL, active: true });
    }
  } else if (!String(targetTab.url || "").includes("/ffa/mshop/homepage/")) {
    targetTab = await chrome.tabs.update(targetTab.id, { active: true, url: DOUDIAN_OVERVIEW_URL });
  }

  if (triggerButton) {
    triggerButton.disabled = true;
    triggerButton.textContent = "正在准备抖店…";
  }
  const readyTab = await waitForDoudianOverview(Number(targetTab?.id));
  const response = await chrome.runtime.sendMessage({
    type: "sync-current-page",
    source_only: "doudian",
    target_tab_id: Number(readyTab.id),
    purpose: "start-store-scan",
  });
  if (response?.ok !== true || !response?.result) {
    const error = new Error(response?.error || "当前抖店读取失败");
    error.code = response?.error_code || response?.code || "";
    throw error;
  }
  const acceptedStoreKey = String(response.result?.store?.key || "").trim().toLowerCase();
  if (!/^[a-z0-9_-]{8,80}$/.test(acceptedStoreKey)) {
    const error = new Error("这个页面暂时不能用于经营判断。请刷新抖店首页后重试。");
    error.code = "STORE_IDENTITY_UNRESOLVED";
    throw error;
  }

  // Use only the store returned by this accepted current-page receipt. This
  // remains compatible with older Agents while newer Agents perform the same
  // selection atomically during /push.
  const backendAlreadySelected = response.result.store_auto_confirmed === true
    && String(response.result.selected_store_key || "").trim().toLowerCase() === acceptedStoreKey;
  if (!backendAlreadySelected) {
    await bridgeFetch("/stores/select", {
      method: "POST",
      body: JSON.stringify({ store_key: acceptedStoreKey }),
    });
  }
  await loadDashboard();
  if (selectedStoreKey !== acceptedStoreKey) {
    selectedStoreKey = "";
    selectedQianchuanAccount = "";
    throw new Error("抖店已读取，但经营数据还没准备好。请点一次刷新后重试。");
  }
  return acceptedStoreKey;
}

async function runQuickScan(triggerButton = null, options = {}) {
  const refreshingCoreData = options.purpose === "refresh_core_data";
  const pageIds = normalizeCoreRefreshPageIds(options.pageIds, { fallback: !refreshingCoreData });
  if (refreshingCoreData && !pageIds.length) {
    throw new Error("未取得精确待刷新页面，已停止扩大采集范围。请刷新连接状态后重试。");
  }
  try {
    await ensureCurrentStoreForScan(triggerButton);
  } catch (error) {
    throw new Error(simpleStorePreparationError(error));
  }
  navigateToWorkspaceElement("scan-card", {
    workspaceKey: "data-scan",
    title: refreshingCoreData ? "刷新核心经营数据" : "3 分钟快速巡店",
    subtitle: refreshingCoreData
      ? `只刷新当前过期或缺失的 ${pageIds.length} 个核心页面；不会扩大到整店巡检。`
      : "只检查经营概览、订单、商品和商城四类核心页面；完整巡店仍可在本页单独启动。",
  });
  if (triggerButton) {
    triggerButton.disabled = true;
    triggerButton.textContent = refreshingCoreData ? "正在刷新核心数据…" : "正在启动快速巡店…";
  }
  document.getElementById("scan-detail").textContent = `正在准备 ${pageIds.length} 个核心页面；完成后只展示异常和下一步。`;
  try {
    const response = await chrome.runtime.sendMessage({
      type: "start-full-scan",
      scan_scope: "quick",
      store_key: selectedStoreKey,
      account_key: "",
      page_ids: pageIds,
    });
    if (response?.ok !== true || response.started !== true) {
      if (response?.code === "SCAN_BUSY") {
        const busyMessage = response.message || "已有巡检正在进行，请等待当前进度完成。";
        document.getElementById("scan-detail").textContent = busyMessage;
        if (triggerButton) triggerButton.textContent = "巡检正在进行，请等待";
        await loadDashboard();
        return response;
      }
      const error = new Error(response?.error || response?.message || "快速巡店未能启动");
      error.code = response?.error_code || response?.code || "";
      throw error;
    }
    await loadDashboard();
    return response;
  } finally {
    if (triggerButton) triggerButton.disabled = false;
  }
}

function normalizeTargetedScanReturn(value = {}) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const targetId = String(value.target_id || "").trim();
  const runId = String(value.run_id || "").trim();
  const scope = String(value.scope || "").trim();
  if (targetId !== "product-operating-graph" || !runId || scope !== "product_graph") return null;
  return { target_id: targetId, run_id: runId, scope };
}

function targetedScanReturnMatches(scan = {}, target = {}) {
  const normalized = normalizeTargetedScanReturn(target);
  return Boolean(
    normalized
    && String(scan.run_id || "") === normalized.run_id
    && String(scan.scope || "") === normalized.scope,
  );
}

async function maybeReturnFromTargetedScan(scan = {}, generation = 0) {
  if (!["completed", "partial", "cancelled", "interrupted", "error"].includes(scan.status)) return;
  const stored = await chrome.storage.local.get("scanReturnTarget");
  if (!dashboardLoadIsCurrent(generation)) return false;
  const target = normalizeTargetedScanReturn(stored.scanReturnTarget);
  // A terminal snapshot from another run is not authority to clear the current
  // marker. This is common just after a new targeted scan starts, while the
  // background dashboard still exposes the previous run for one refresh.
  if (!target || !targetedScanReturnMatches(scan, target)) return false;

  // Re-read immediately before deletion. A newer refresh or targeted run may
  // have replaced the marker while the first storage read was pending.
  const latestStored = await chrome.storage.local.get("scanReturnTarget");
  if (!dashboardLoadIsCurrent(generation)) return false;
  const latestTarget = normalizeTargetedScanReturn(latestStored.scanReturnTarget);
  if (!latestTarget || latestTarget.run_id !== target.run_id || latestTarget.scope !== target.scope) return false;
  await chrome.storage.local.remove("scanReturnTarget");
  // Once this generation has claimed the exact marker, finish the navigation.
  // Returning here after deletion would consume the marker without completing
  // its one-shot handoff when another dashboard refresh starts mid-remove.
  navigateToWorkspaceElement("product-operating-graph", {
    role: "货架商品",
    workspaceKey: "shelf-products",
  });
  const note = document.getElementById("product-graph-note");
  if (!note) return;
  if (scan.status === "completed") {
    note.textContent ||= "单品链补采已完成，已按最新商品身份、库存和计划映射重新诊断。";
  } else {
    note.textContent = `单品链补采${scan.status === "partial" ? "部分完成" : "未完成"}：${scan.error || "请按上方唯一主按钮补齐失败页面。"}`;
  }
  return true;
}

function agentWriteBlockReason(recovery = {}) {
  if (recovery.action === "retry_connection") {
    const reason = String(recovery.banner_title || recovery.title || "安全会话正在恢复")
      .replace(/[。；;]+$/, "");
    return `${reason}；系统正在自动复核，请稍后重试。`;
  }
  if (recovery.action === "open_extension_manager") {
    const reason = String(recovery.banner_title || recovery.title || "扩展尚未通过本机连接验收")
      .replace(/[。；;]+$/, "");
    return `${reason}；请先在扩展管理页重新加载。`;
  }
  return DEFAULT_AGENT_WRITE_BLOCK_MESSAGE;
}

function agentWriteGateOpen() {
  return agentConnectionState === "online";
}

function currentAgentWriteBlockMessage() {
  return agentConnectionState === "checking"
    ? CHECKING_AGENT_WRITE_BLOCK_MESSAGE
    : agentWriteBlockMessage;
}

function markAgentWriteControl(control) {
  if (!control) return;
  control.dataset.agentWrite = "true";
  if (!agentWriteGateOpen()) {
    if (!Object.hasOwn(control.dataset, "agentOfflineWasDisabled")) {
      control.dataset.agentOfflineWasDisabled = String(control.disabled === true);
    }
    control.disabled = true;
    control.setAttribute("aria-disabled", "true");
    if (!Object.hasOwn(control.dataset, "agentOfflinePreviousTitle")) {
      control.dataset.agentOfflinePreviousTitle = control.getAttribute("title") || "";
    }
    control.title = currentAgentWriteBlockMessage();
  }
}

function registerAgentWriteControls(root = document) {
  AGENT_WRITE_CONTROL_IDS.forEach((id) => markAgentWriteControl(document.getElementById(id)));
  root.querySelectorAll?.(AGENT_WRITE_DYNAMIC_SELECTOR).forEach(markAgentWriteControl);
}

function setAgentAvailability(online, detail = "", recovery = {}) {
  agentConnectionState = online ? "online" : "offline";
  agentWriteBlockMessage = online ? DEFAULT_AGENT_WRITE_BLOCK_MESSAGE : agentWriteBlockReason(recovery);
  document.body.classList.toggle("agent-offline", !online);
  const banner = document.getElementById("agent-offline-banner");
  if (banner) banner.hidden = online;
  const detailNode = document.getElementById("agent-offline-detail");
  const recoveryAction = ["open_extension_manager", "retry_connection"].includes(recovery.action)
    ? recovery.action
    : "repair_agent";
  const recoveryButton = document.getElementById("sidepanel-repair-agent");
  if (!online && detailNode) {
    detailNode.textContent = recoveryAction === "retry_connection"
      ? `${detail || "安全会话正在恢复"}。系统会在窗口重新获得焦点后自动复核，当前数据只供查看。`
      : recoveryAction === "open_extension_manager"
      ? `${detail || "扩展尚未通过本机连接验收"}。完成重新加载后再检测；当前数据只供查看。`
      : detail
        ? `连接失败：${detail}。先修复本地 Agent，再重新检测；当前数据只供查看。`
        : "先修复本地 Agent，再重新检测连接；当前数据只供查看。";
    banner?.querySelector("strong")?.replaceChildren(recovery.banner_title || "本地 Agent 已断开，写入与执行已冻结");
    if (recoveryButton) {
      recoveryButton.dataset.action = recoveryAction;
      recoveryButton.textContent = recovery.action_label || (recoveryAction === "retry_connection"
        ? "立即重试"
        : recoveryAction === "open_extension_manager" ? "打开扩展管理页" : "修复本地 Agent");
    }
  }
  registerAgentWriteControls();
  document.querySelectorAll("[data-agent-write]").forEach((control) => {
    if (!agentWriteGateOpen()) {
      markAgentWriteControl(control);
      return;
    }
    const wasDisabled = control.dataset.agentOfflineWasDisabled;
    if (wasDisabled === "false") control.disabled = false;
    delete control.dataset.agentOfflineWasDisabled;
    control.removeAttribute("aria-disabled");
    if (Object.hasOwn(control.dataset, "agentOfflinePreviousTitle")) {
      const previousTitle = control.dataset.agentOfflinePreviousTitle;
      if (previousTitle) control.title = previousTitle;
      else control.removeAttribute("title");
      delete control.dataset.agentOfflinePreviousTitle;
    }
  });
}

async function openAgentRepairGuide() {
  const response = await chrome.runtime.sendMessage({ type: "repair-agent" });
  if (!response?.ok) throw new Error(response?.error || "修复指引打开失败");
  return response;
}

async function bridgeFetch(path, options = {}) {
  const requestGeneration = dashboardLoadGeneration;
  const method = String(options.method || "GET").toUpperCase();
  if (!agentWriteGateOpen() && method !== "GET") {
    throw new Error(currentAgentWriteBlockMessage());
  }
  const headers = { ...(options.headers || {}) };
  if (options.method && options.method !== "GET") headers["X-Dian-Agent"] ||= "2";
  if (options.body) headers["Content-Type"] ||= "application/json";
  try {
    return await bridgeAuthClient.fetchJson(path, { ...options, headers });
  } catch (error) {
    // A single read-only module can fail while the independently authenticated
    // background health probe is still healthy. Do not turn that module error
    // into a global offline verdict. Mutating requests remain fail-closed on
    // transport loss; dashboard reads are classified after bridge acceptance.
    if (method !== "GET" && !Number(error?.status) && requestGeneration === dashboardLoadGeneration) {
      setAgentAvailability(false, error?.message || "本地服务无响应");
    }
    throw error;
  }
}

function dashboardReadErrorIsTransient(error = {}) {
  const status = Number(error?.status || 0);
  return !status || [408, 425, 429].includes(status) || status >= 500;
}

function waitForDashboardReadRetry() {
  return new Promise((resolve) => setTimeout(resolve, DASHBOARD_READ_RETRY_DELAY_MS));
}

function dashboardTimeoutError(label = "工作台读取") {
  const error = new Error(`${label}超过 ${Math.round(DASHBOARD_READ_TIMEOUT_MS / 1000)} 秒`);
  error.code = "DASHBOARD_READ_TIMEOUT";
  return error;
}

function dashboardTimedPromise(operation, label, onTimeout = () => undefined) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      onTimeout();
      reject(dashboardTimeoutError(label));
    }, DASHBOARD_READ_TIMEOUT_MS);
  });
  return Promise.race([operation, timeout]).finally(() => clearTimeout(timer));
}

function dashboardReadAttempt(path) {
  const controller = new AbortController();
  return dashboardTimedPromise(
    bridgeFetch(path, { cache: "no-store", signal: controller.signal }),
    `读取 ${path}`,
    () => controller.abort(),
  );
}

function dashboardRuntimeMessage(message, label) {
  return dashboardTimedPromise(chrome.runtime.sendMessage(message), label);
}

async function dashboardRead(path, generation) {
  try {
    return await dashboardReadAttempt(path);
  } catch (error) {
    if (!dashboardReadErrorIsTransient(error) || !dashboardLoadIsCurrent(generation)) throw error;
    await waitForDashboardReadRetry();
    if (!dashboardLoadIsCurrent(generation)) throw error;
    return dashboardReadAttempt(path);
  }
}

function dashboardBridgeReceiptNeedsConfirmation(bridge = {}) {
  return !(
    bridge
    && typeof bridge === "object"
    && bridge?.ok === true
    && bridge?.liveness_ok === true
    && bridge?.authenticated === true
    && bridge?.version_match === true
  );
}

function dashboardBridgeAuthenticationRecoverable(bridge = {}) {
  const errorCode = String(bridge?.error_code || "").trim().toLowerCase();
  return Boolean(
    bridge
    && typeof bridge === "object"
    && bridge?.liveness_ok === true
    && bridge?.version_match === true
    && bridge?.authenticated !== true
    && (!errorCode || RECOVERABLE_AGENT_AUTH_ERROR_CODES.has(errorCode))
  );
}

async function dashboardPrimeBridgeAuthentication(generation) {
  const status = await dashboardReadAttempt("/auth/status");
  if (!dashboardLoadIsCurrent(generation)) {
    throw new Error("工作台连接核验已被较新的刷新取代");
  }
  const runtimeVersion = String(chrome.runtime.getManifest?.()?.version || "").trim();
  const requiredVersion = String(status?.required_extension_version || "").trim();
  const sessionVersion = String(status?.session_extension_version || "").trim();
  const sessionSubject = String(status?.session_subject || "").trim().toLowerCase();
  if (
    status?.authenticated !== true
    || status?.client_kind !== "browser_extension"
    || !runtimeVersion
    || requiredVersion !== runtimeVersion
    || sessionVersion !== runtimeVersion
    || sessionSubject !== String(chrome.runtime.id || "").trim().toLowerCase()
  ) {
    const error = new Error("Agent 与当前扩展版本尚未形成一致的认证回执");
    error.code = "agent_extension_version_contract_conflict";
    throw error;
  }
  return status;
}

async function dashboardBridgeReceiptRead(generation) {
  let firstResponse;
  try {
    firstResponse = await dashboardRuntimeMessage({ type: "test-bridge" }, "核验本地 Agent 连接");
    if (!firstResponse || typeof firstResponse !== "object") {
      throw new Error("扩展后台暂未返回 Agent 连接回执");
    }
    if (!dashboardBridgeReceiptNeedsConfirmation(firstResponse)) return firstResponse;
  } catch (error) {
    if (!dashboardLoadIsCurrent(generation)) throw error;
  }
  // A live Agent with a matching runtime can briefly retain a stale worker
  // session after an extension reload. Prime one protected GET from this live
  // workbench before the worker's final confirmation. The protected request
  // must authenticate and prove the same required version; failure remains
  // fail-closed and can never open the global write gate.
  if (dashboardBridgeAuthenticationRecoverable(firstResponse)) {
    await dashboardPrimeBridgeAuthentication(generation).catch(() => undefined);
  }
  if (!dashboardLoadIsCurrent(generation)) {
    throw new Error("工作台连接核验已被较新的刷新取代");
  }
  await waitForDashboardReadRetry();
  if (!dashboardLoadIsCurrent(generation)) {
    throw new Error("工作台连接核验已被较新的刷新取代");
  }
  try {
    const confirmation = await dashboardRuntimeMessage({ type: "test-bridge" }, "重新核验本地 Agent 连接");
    if (confirmation && typeof confirmation === "object") return confirmation;
    throw new Error("扩展后台暂未返回 Agent 连接回执");
  } catch (error) {
    if (firstResponse && typeof firstResponse === "object") return firstResponse;
    throw error;
  }
}

async function dashboardBackgroundRead(generation) {
  let firstError;
  try {
    const response = await dashboardRuntimeMessage({ type: "get-dashboard" }, "读取浏览器工作台状态");
    if (response?.ok === true && response?.dashboard && typeof response.dashboard === "object") return response;
    throw new Error(response?.error || "扩展后台暂未返回工作台状态");
  } catch (error) {
    firstError = error;
    if (!dashboardLoadIsCurrent(generation)) throw error;
  }
  await waitForDashboardReadRetry();
  if (!dashboardLoadIsCurrent(generation)) throw firstError;
  const confirmation = await dashboardRuntimeMessage({ type: "get-dashboard" }, "重新读取浏览器工作台状态");
  if (confirmation?.ok === true && confirmation?.dashboard && typeof confirmation.dashboard === "object") {
    return confirmation;
  }
  throw new Error(confirmation?.error || firstError?.message || "扩展后台暂未返回工作台状态");
}

async function runDashboardReadPool(tasks, { concurrency = DASHBOARD_READ_CONCURRENCY } = {}) {
  const queue = Array.isArray(tasks) ? tasks : [];
  const limit = Math.max(1, Math.min(Number(concurrency) || 1, queue.length || 1));
  const results = new Array(queue.length);
  let nextIndex = 0;

  async function worker() {
    while (nextIndex < queue.length) {
      const index = nextIndex;
      nextIndex += 1;
      try {
        results[index] = { status: "fulfilled", value: await queue[index]() };
      } catch (reason) {
        results[index] = { status: "rejected", reason };
      }
    }
  }

  await Promise.all(Array.from({ length: limit }, () => worker()));
  return results;
}

function renderConnection(ok, title, detail, recovery = {}) {
  setAgentAvailability(ok, ok ? "" : detail, recovery);
  const element = document.getElementById("connection");
  element.className = `connection ${ok ? "ok" : "error"}`;
  element.querySelector("strong").textContent = title;
  element.querySelector("p").textContent = detail;
  const topbar = document.querySelector(".workspace-agent-state");
  topbar.className = `workspace-agent-state ${ok ? "ok" : "error"}`;
  // Keep the compact topbar consistent with the detailed recovery card. A
  // live Agent that needs extension reload/pairing is not an offline Agent.
  document.getElementById("workspace-agent-label").textContent = ok ? "本地 Agent 已连接" : title;
}

function dashboardBridgeReceiptFailure(result = {}, response = {}) {
  if (result?.status === "fulfilled" && response && typeof response === "object") {
    return null;
  }
  const reason = result?.reason?.message || response?.error || "扩展后台没有返回 Agent 连接回执";
  return {
    title: "浏览器扩展后台不可用",
    headline: "需要重新加载扩展",
    detail: `${reason}。请在扩展管理页重新加载店策 Agent 后再检测`,
    banner_title: "扩展后台不可用，巡店、同步与执行已冻结",
    action: "open_extension_manager",
    action_label: "打开扩展管理页",
  };
}

function dashboardBridgeAccepted(backgroundFailure, bridge = {}) {
  return Boolean(
    !backgroundFailure
    && bridge?.ok === true
    && bridge?.liveness_ok === true
    && bridge?.authenticated === true
    && bridge?.version_match === true
  );
}

function dashboardBridgeFailure(bridge = {}, fallback = "") {
  if (bridge?.liveness_ok === true && bridge?.version_match === false) {
    return {
      title: "Agent 与扩展版本不一致",
      headline: "需要重新加载扩展",
      detail: bridge.reload_requested === true
        ? "已请求重新加载本地扩展；若页面没有自动恢复，请在扩展管理页点一次“重新加载”"
        : "请在扩展管理页点一次“重新加载”；若仍失败，再运行 Repair Dian Agent",
      banner_title: "Agent 与扩展版本不一致，写入与执行已冻结",
      action: "open_extension_manager",
      action_label: "打开扩展管理页",
    };
  }
  if (bridge?.liveness_ok === true && bridge?.authenticated !== true) {
    if (dashboardBridgeAuthenticationRecoverable(bridge)) {
      return {
        title: "Agent 已运行，安全会话正在恢复",
        headline: "连接恢复中",
        detail: bridge?.error || "扩展后台正在重新建立本机认证会话",
        banner_title: "正在恢复扩展与本地 Agent 的安全会话",
        action: "retry_connection",
        action_label: "立即重试",
        transient: true,
      };
    }
    return {
      title: "Agent 已运行，扩展未配对",
      headline: "本机配对或认证不可用",
      detail: bridge?.error || "当前扩展不在本机可信登记中，请运行 Repair Dian Agent 后重新检测",
      banner_title: "扩展尚未通过本机认证，写入与执行已冻结",
      action: "repair_agent",
      action_label: "修复本地 Agent",
    };
  }
  if (bridge?.liveness_ok === false) {
    return {
      title: "本地 Agent 未启动",
      headline: "暂时无法连接",
      detail: fallback || "点击“修复本地 Agent”打开安装版恢复指引",
      banner_title: "本地 Agent 已断开，写入与执行已冻结",
      action: "repair_agent",
      action_label: "修复本地 Agent",
    };
  }
  const receiptFields = ["ok", "liveness_ok", "authenticated", "version_match"];
  if (receiptFields.some((field) => !Object.hasOwn(bridge, field))) {
    return {
      title: "Agent 连接验收未完成",
      headline: "需要重新确认连接",
      detail: "扩展后台返回的连接回执不完整。请先重新检测；若仍未恢复，再在扩展管理页重新加载",
      banner_title: "Agent 连接状态待确认，写入与执行已冻结",
      action: "open_extension_manager",
      action_label: "打开扩展管理页",
    };
  }
  return {
    title: "Agent 连接验收未通过",
    headline: "需要重新确认连接",
    detail: fallback || "请先重新检测；若仍未恢复，再在扩展管理页重新加载",
    banner_title: "Agent 连接未通过安全验收，写入与执行已冻结",
    action: "open_extension_manager",
    action_label: "打开扩展管理页",
  };
}

function applyExperienceMode(value, { persist = true } = {}) {
  experienceMode = globalThis.DianSimpleExperience.normalizeMode(value);
  document.body.dataset.experienceMode = experienceMode;
  document.body.classList.remove("simple-show-batch", "simple-show-receipt", "simple-show-industry", "simple-show-ai", "simple-show-report", "simple-show-settings", "simple-show-version");
  clearWorkspacePageFocus();
  if (!qianchuanSyncDockCompactPreferenceSet) {
    setQianchuanSyncDockCompact(experienceMode === "simple", { persist: false });
  }
  document.querySelectorAll("[data-experience-mode-value]").forEach((button) => {
    const active = button.dataset.experienceModeValue === experienceMode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  if (experienceMode === "simple") {
    const livePacing = document.getElementById("live-pacing-card");
    if (livePacing) livePacing.open = true;
    document.querySelectorAll("#content-workbench details").forEach((details) => { details.open = false; });
  }
  const currentTarget = document.getElementById(currentWorkspaceTarget);
  if (currentTarget) {
    revealSimpleWorkspaceTarget(currentTarget);
    focusWorkspacePage(currentTarget);
  }
  if (persist) chrome.storage.local.set({ [EXPERIENCE_MODE_KEY]: experienceMode }).catch(() => undefined);
}

function renderSimpleJourney(onboarding = currentOnboarding || {}, connectionGuide = currentConnectionGuide || {}) {
  const root = document.getElementById("simple-start");
  if (!root || !globalThis.DianSimpleExperience) return;
  const journey = globalThis.DianSimpleExperience.deriveJourney(onboarding, connectionGuide);
  currentSimpleJourney = journey;
  root.dataset.dataState = journey.data_state || "never";
  document.body.classList.toggle("onboarding-complete", journey.complete);
  root.classList.toggle("completed", journey.complete);
  document.getElementById("simple-start-title").textContent = journey.complete ? "已经准备好了" : "打开抖店，直接开始";
  document.getElementById("simple-start-progress").textContent = journey.complete ? "已完成" : `${journey.completed} / ${journey.total}`;
  document.getElementById("simple-start-steps").replaceChildren(...journey.steps.map((item, index) => {
    const step = document.createElement("article");
    step.className = item.complete ? "complete" : item.current ? "current" : "pending";
    const marker = document.createElement("b"); marker.textContent = item.complete ? "✓" : String(index + 1);
    const copy = document.createElement("div");
    const title = document.createElement("strong"); title.textContent = item.label;
    const detail = document.createElement("small"); detail.textContent = item.detail;
    copy.append(title, detail);
    step.append(marker, copy);
    return step;
  }));
  document.getElementById("simple-start-next-title").textContent = journey.action.title;
  document.getElementById("simple-start-next-detail").textContent = journey.action.detail;
  document.getElementById("simple-start-note").textContent = journey.optional_note;
  const button = document.getElementById("simple-start-action");
  button.dataset.action = journey.action.id;
  button.textContent = journey.action.label;
  button.disabled = journey.action.id === "none";
}

function renderSimpleAgentError(detail = "请修复本地 Agent，然后重新检测连接。", recovery = {}) {
  const root = document.getElementById("simple-start");
  if (!root) return;
  root.classList.remove("completed");
  document.body.classList.remove("onboarding-complete");
  document.getElementById("simple-start-title").textContent = "先恢复本地连接";
  document.getElementById("simple-start-progress").textContent = "需要处理";
  document.getElementById("simple-start-next-title").textContent = recovery.headline || "本地 Agent 暂时没有响应";
  document.getElementById("simple-start-next-detail").textContent = detail;
  const button = document.getElementById("simple-start-action");
  button.dataset.action = recovery.action || "repair_agent";
  button.textContent = recovery.action_label || "修复本地 Agent";
  button.disabled = false;
}

function openSimpleBatchPlan() {
  const allowedModes = ["full_domain", "chengfang", "suixintui"];
  currentBatchPlanMode = allowedModes.includes(currentPromotionPlanFilters.mode) ? currentPromotionPlanFilters.mode : "chengfang";
  document.getElementById("batch-plan-mode-label").textContent = {
    full_domain: "全域推广 · 批量创建",
    chengfang: "乘方推广 · 批量创建",
    suixintui: "随心推 · 批量创建",
  }[currentBatchPlanMode];
  document.getElementById("batch-plan-review-summary").textContent = `${promotionPlanModeLabel(currentBatchPlanMode)} · 选择账户、填写推广对象和预算后生成本地草稿。`;
  document.body.classList.add("simple-show-batch");
  navigateToWorkspaceElement("batch-plan-preparation", {
    role: "直播投放",
    title: "生成计划草稿",
    subtitle: "先核对账户、推广对象和经营参数，再生成不会写入平台的本地草稿。",
  });
}

function scanReceiptFromStatus(scan = {}) {
  const plan = DianAgentScanScopePolicy.receiptPlan(scan);
  const allResults = (scan.results || []).filter((item) => item && typeof item === "object").map((item) => {
    const score = Math.max(0, Math.min(100, Number(item.quality?.score || 0)));
    const collectionIncomplete = item.collection_complete === false;
    return {
      ...item,
      source: item.source || (String(item.id || "").startsWith("qianchuan") ? "qianchuan" : "doudian"),
      quality_score: score,
      metric_count: Number(item.quality?.metric_count || 0),
      row_count: Number(item.quality?.row_count || 0),
      warning_code: String(item.warning_code || ""),
      warning: String(item.warning || ""),
      needs_review: Boolean(item.ok) && (score < 70 || collectionIncomplete),
    };
  });
  const resultById = new Map(allResults.filter((item) => item.id).map((item) => [String(item.id), item]));
  const results = plan.expected_page_ids.length
    ? plan.expected_page_ids.map((pageId) => resultById.get(pageId)).filter(Boolean)
    : allResults;
  const total = plan.expected_page_ids.length || Math.max(Number(scan.total || 0), results.length);
  const success = results.filter((item) => item.ok && item.collection_complete !== false).length;
  const failed = results.filter((item) => !item.ok).length;
  const incomplete = results.filter((item) => item.ok && item.collection_complete === false);
  const lowQuality = results.filter((item) => item.ok && item.quality_score < 70);
  const needsReview = results.filter((item) => item.needs_review).length;
  const coverageRate = total ? Math.round(results.length / total * 100) : 0;
  const running = scan.status === "running";
  const ready = scan.status === "completed" && plan.contract_complete && coverageRate === 100 && failed === 0 && needsReview === 0;
  return {
    scan_status: scan.status || "idle",
    readiness: running ? "running" : ready ? "ready" : results.length ? "attention" : "empty",
    readiness_label: running ? "正在采集" : ready ? "数据可用于分析" : results.length ? "需要补采或复核" : "等待巡查",
    account_label: results.find((item) => item.account_label)?.account_label || "",
    finished_at: Number(scan.finished_at || 0),
    planned_page_ids: [...plan.planned_page_ids],
    expected_page_ids: [...plan.expected_page_ids],
    missing_contract_page_ids: [...plan.missing_contract_page_ids],
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
      failed ? `${failed} 个页面读取失败，请按错误类型完成下方唯一恢复动作。` : "",
      incomplete.length
        ? `${incomplete.length} 个页面采集未完整：${[...new Set(incomplete.map((item) => [item.warning_code, item.warning].filter(Boolean).join(" · ") || "COLLECTION_INCOMPLETE"))].join("；")}。`
        : "",
      lowQuality.length ? `${lowQuality.length} 个页面质量分低于 70，相关建议需要人工复核。` : "",
      total && results.length < total && !running ? `巡查仅覆盖 ${results.length}/${total} 个页面。` : "",
      plan.missing_contract_page_ids.length
        ? `全店巡检范围缺少 ${plan.missing_contract_page_ids.join("、")}，不能按 100% 完成。`
        : "",
    ].filter(Boolean),
    results,
  };
}

function scanRecoveryAction(item = {}) {
  const message = String(item.error || item.message || "");
  let code = String(item.error_code || item.code || "").trim().toUpperCase();
  if (!code && /登录已失效|完成登录/.test(message)) code = "LOGIN_REQUIRED";
  if (!code && /未识别当前千川账号/.test(message)) code = "ACCOUNT_UNRESOLVED";
  if (!code && /锁定账号不一致|所选巡查账号不一致/.test(message)) code = "ACCOUNT_MISMATCH";
  if (!code && /所属店铺与已选店铺不一致|请求巡检的店铺与当前已选店铺不一致/.test(message)) code = "STORE_MISMATCH";
  if (!code && /没有可靠店铺身份|尚未识别当前店铺/.test(message)) code = "STORE_IDENTITY_UNRESOLVED";
  if (!code && /身份冲突|IDENTITY_CONFLICT/.test(message)) code = /千川/.test(message) ? "ACCOUNT_IDENTITY_CONFLICT" : "STORE_IDENTITY_CONFLICT";
  if (!code && /店铺目录存在多个已选店铺|店铺目录的选择状态不一致/.test(message)) code = "STORE_SELECTION_CONFLICT";
  if (!code && /店铺尚未在本地目录中确认/.test(message)) code = "STORE_SELECTION_UNCONFIRMED";
  const source = String(item.source || (/千川/.test(message) ? "qianchuan" : "")).toLowerCase();
  const loginUrl = source === "qianchuan"
    ? "https://qianchuan.jinritemai.com/"
    : "https://fxg.jinritemai.com/ffa/mshop/homepage/index";
  const action = (label, kind, options = {}) => ({ code, label, kind, ...options });
  if (code === "AGENT_OFFLINE") return action("修复本地 Agent", "repair_agent");
  if (code === "LOGIN_REQUIRED") return action(source === "qianchuan" ? "打开千川登录" : "打开抖店登录", "open_url", { url: loginUrl });
  if (["ACCOUNT_UNRESOLVED", "ACCOUNT_IDENTITY_CONFLICT"].includes(code)) return action("打开对应千川页面", "open_url", { url: "https://qianchuan.jinritemai.com/" });
  if (code === "ACCOUNT_MISMATCH") return action("切换回已选账户", "open_url", { url: "https://qianchuan.jinritemai.com/" });
  if (["STORE_MISMATCH", "STORE_IDENTITY_UNRESOLVED", "STORE_IDENTITY_CONFLICT", "STORE_SELECTION_INVALID", "STORE_SELECTION_CONFLICT", "STORE_SELECTION_UNCONFIRMED", "STORE_SELECTION_MISMATCH"].includes(code)) {
    return action("重新打开并巡店", "prepare_store");
  }
  if (code === "AUTH_EXPIRED") return action("重新授权", "navigate", { targetId: "oceanengine-oauth-card" });
  if (["READ_PERMISSION_MISSING", "PERMISSION_DENIED"].includes(code)) {
    return action("补充读取权限", "navigate", { targetId: "oceanengine-oauth-card" });
  }
  if (code === "WRITE_PERMISSION_MISSING") {
    return action("继续只读预演", "navigate", { targetId: "promotion-plan-center", workspaceKey: "promotion-overview" });
  }
  if (["PAGE_CONTRACT_MISSING", "IDENTITY_CONTRACT_MISSING"].includes(code)) {
    return action("检查商品 ID / 库存列", "navigate", { targetId: "product-operating-graph", workspaceKey: "shelf-products" });
  }
  if (code === "EXECUTION_RESULT_UNKNOWN") {
    return action("同步当前计划状态", "navigate", { targetId: "promotion-operation-log", workspaceKey: "promotion-operation-log" });
  }
  if (code === "READBACK_FAILED") {
    return action("重新回读", "navigate", { targetId: "promotion-operation-log", workspaceKey: "promotion-operation-log" });
  }
  if (code === "VALIDATION_BLOCKED") {
    return action("补齐具体缺口", "navigate", { targetId: "today-task-center", workspaceKey: "today-tasks" });
  }
  return action(item.id ? "只重试这一页" : "重试本次巡检", item.id ? "retry_page" : "retry_scan");
}

async function runScanRecovery(recovery, item, button) {
  if (recovery.kind === "repair_agent") {
    await openAgentRepairGuide();
    return;
  }
  if (recovery.kind === "open_url") {
    await chrome.tabs.create({ url: recovery.url, active: true });
    return;
  }
  if (recovery.kind === "prepare_store") {
    await ensureCurrentStoreForScan(button);
    await loadDashboard();
    return;
  }
  if (recovery.kind === "navigate") {
    navigateToWorkspaceElement(recovery.targetId, {
      workspaceKey: recovery.workspaceKey || "",
      focusElement: null,
      highlight: true,
    });
    return;
  }
  const pageIds = recovery.kind === "retry_scan"
    ? (Array.isArray(item.targeted_page_ids) ? item.targeted_page_ids : null)
    : [item.id];
  const response = await chrome.runtime.sendMessage({
    type: "start-full-scan",
    scan_scope: recovery.kind === "retry_scan" ? item.scope || "full" : undefined,
    store_key: selectedStoreKey,
    page_ids: pageIds,
    account_key: selectedQianchuanAccount,
  });
  if (response?.ok !== true || response.started !== true) {
    if (response?.code === "SCAN_BUSY") {
      button.textContent = response.message || "巡检正在进行，请等待";
      await loadDashboard();
      return response;
    }
    const error = new Error(response?.error || "页面重试未能启动");
    error.code = response?.error_code || response?.code || item.error_code || "";
    throw error;
  }
  button.textContent = recovery.kind === "retry_scan" ? "已重新启动巡检" : "已启动本页重试";
  await loadDashboard();
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
      : `${LABELS[item.source] || item.source} · 质量 ${item.quality_score || 0} · ${item.row_count || 0} 行 · ${item.metric_count || 0} 项指标${item.collection_complete === false ? ` · ${[item.warning_code, item.warning].filter(Boolean).join(" · ") || "采集未完整"}` : ""}`;
    cardRow.append(title, tag, detail);
    if (!item.ok && item.id) {
      const recovery = scanRecoveryAction(item);
      const recoveryButton = document.createElement("button");
      recoveryButton.type = "button";
      recoveryButton.textContent = recovery.label;
      recoveryButton.dataset.recoveryCode = recovery.code || "NETWORK_TRANSIENT";
      recoveryButton.setAttribute("aria-label", `${recovery.label}：${item.label || item.id}`);
      recoveryButton.addEventListener("click", async () => {
        recoveryButton.disabled = true;
        recoveryButton.textContent = recovery.kind === "retry_page" ? "正在重试本页…" : "正在打开…";
        try {
          await runScanRecovery(recovery, item, recoveryButton);
        } catch (error) {
          detail.textContent = `恢复失败：${error?.message || "请检查当前页面后再试"}`;
        } finally {
          recoveryButton.disabled = false;
          if (recoveryButton.textContent === "正在重试本页…" || recoveryButton.textContent === "正在打开…") {
            recoveryButton.textContent = recovery.label;
          }
        }
      });
      cardRow.append(recoveryButton);
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
  if (!DianAgentScanPolicy.shouldAcceptScanSnapshot(latestFullScanSnapshot, scan)) return false;
  latestFullScanSnapshot = { ...scan };
  const running = scan.status === "running";
  const productGraphScan = scan.scope === "product_graph";
  const quickScan = scan.scope === "quick";
  const scanLabel = productGraphScan ? "单品链补采" : quickScan ? "快速巡店" : "巡检";
  document.getElementById("scan-title").textContent = quickScan ? "3 分钟快速巡店" : productGraphScan ? "单品链补采" : "全店自动巡检";
  fullScanRunning = running;
  // A full scan may legitimately exceed five minutes. Only stop when its
  // persisted heartbeat has made no progress for the whole timeout window.
  const heartbeatAt = Number(scan.heartbeat_at || scan.started_at || 0);
  if (running && heartbeatAt > 0 && Date.now() - heartbeatAt > SCAN_TIMEOUT_MS) {
    chrome.runtime.sendMessage({ type: "cancel-full-scan" }).catch(() => undefined);
    document.getElementById("scan-detail").textContent = `巡检连续 ${SCAN_TIMEOUT_MS / 60000} 分钟没有新进展，已安全停止；可从未完成页面继续`;
    return true;
  }
  const state = document.getElementById("scan-state");
  const labels = { idle: "未运行", running: "巡检中", completed: "已完成", partial: "部分完成", cancelled: "已停止", interrupted: "已中断", error: "失败" };
  state.textContent = scan.status === "completed" && (quickScan || productGraphScan) ? `${scanLabel}完成` : labels[scan.status] || "未运行";
  state.className = `scan-tag ${running || scan.status === "completed" ? "ok" : ["partial", "interrupted"].includes(scan.status) ? "warn" : scan.status === "error" ? "error" : "idle"}`;
  const plan = DianAgentScanScopePolicy.receiptPlan(scan);
  const expectedPageIds = plan.expected_page_ids;
  const attemptedPageIds = new Set(
    (Array.isArray(scan.attempted_page_ids) && scan.attempted_page_ids.length
      ? scan.attempted_page_ids
      : (scan.results || []).map((item) => item?.id))
      .map((item) => String(item || "")),
  );
  const total = expectedPageIds.length || Math.max(Number(scan.total || 0), Number(scan.index || 0));
  const index = expectedPageIds.length
    ? expectedPageIds.filter((pageId) => attemptedPageIds.has(pageId)).length
    : Number(scan.index || 0);
  document.getElementById("scan-progress-bar").style.width = `${Math.min(100, total ? index / total * 100 : 0)}%`;
  document.getElementById("scan-detail").textContent = running ? `正在${scanLabel}：${scan.current || "准备中"}（${index}/${total}）` : scan.finished_at ? `${scanLabel}：成功 ${scan.success || 0}，失败 ${scan.failed || 0}` : "按清单自动打开页面并采集，不需要 API";
  const persistedPendingPageIds = new Set(
    (Array.isArray(scan.pending_page_ids) ? scan.pending_page_ids : [])
      .map((item) => String(item || "")),
  );
  const pendingCount = expectedPageIds.length
    ? expectedPageIds.filter((pageId) => !attemptedPageIds.has(pageId) || persistedPendingPageIds.has(pageId)).length
    : Array.isArray(scan.pending_page_ids) ? scan.pending_page_ids.length : 0;
  document.getElementById("scan-summary").textContent = scan.error
    ? `失败原因：${scan.error}${pendingCount ? `；还有 ${pendingCount} 页未完成，可断点继续` : ""}`
    : running
      ? `已检查 ${index}/${total} 个页面；巡店结束前不会把占位值当作真实为 0。`
      : quickScan && scan.finished_at
        ? `核心页面通过 ${scan.success || 0}/${total}，失败 ${scan.failed || 0}；下一步只处理异常。`
        : `页面通过 ${scan.success || 0}/${total}，失败 ${scan.failed || 0}，未执行 ${pendingCount}，需复核 ${scan.low_quality || 0}。`;
  document.getElementById("full-scan-button").disabled = running;
  document.getElementById("full-scan-button").textContent = running ? "正在巡店…" : !selectedStoreKey ? "打开抖店并巡店" : "开始全店巡检";
  document.getElementById("cancel-scan-button").hidden = !running;
  const failedRows = (scan.results || []).filter((item) => item && item.ok === false && item.id);
  const overallRecovery = scan.status === "error" && failedRows.length === 0 ? scanRecoveryAction(scan) : null;
  const retryButton = document.getElementById("retry-scan-button");
  const recoveryCanResume = scan.status !== "cancelled" && scan.recovery?.can_resume !== false;
  retryButton.hidden = running || !recoveryCanResume || (!overallRecovery && pendingCount === 0 && (!(scan.failed > 0) || failedRows.length > 0));
  retryButton.textContent = overallRecovery?.label || (pendingCount ? `继续未完成的 ${pendingCount} 页` : "按当前千川账号重试");
  retryButton.dataset.overallRecovery = overallRecovery ? "true" : "false";
  retryButton.dataset.errorCode = overallRecovery?.code || "";
  retryButton.dataset.errorMessage = overallRecovery ? String(scan.error || "").slice(0, 300) : "";
  retryButton.dataset.scanScope = scan.scope || "full";
  retryButton.dataset.pageIds = JSON.stringify(Array.isArray(scan.targeted_page_ids) ? scan.targeted_page_ids : []);
  renderScanReceipt(scanReceiptFromStatus(scan));
  if (running && !scanPoller) scanPoller = setInterval(() => pollFullScan().catch(() => undefined), 1500);
  if (!running && scanPoller) { clearInterval(scanPoller); scanPoller = null; }
  return true;
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
    select.dataset.taskWrite = "strategy";
    markAgentWriteControl(select);
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
  button.dataset.taskWrite = "plan-status";
  markAgentWriteControl(button);
  const next = item.task_updated_at
    ? item.task_status === "todo" ? ["开始处理", "doing"]
      : item.task_status === "doing" ? ["转待观察", "observing"]
        : item.task_status === "observing" ? ["标记完成", "done"] : ["重新打开", "todo"]
    : ["添加到任务", "todo"];
  [button.textContent] = next;
  button.addEventListener("click", async () => {
    button.disabled = true;
    let note = "";
    if (item.task_status === "observing" && next[1] === "done") {
      note = window.prompt("请填写结案说明：做了什么、结果怎样、是否需要继续观察？", "")?.trim() || "";
      if (!note) {
        button.disabled = false;
        return;
      }
    }
    const taskPayload = {
      task_id: String(item.task_id || "").trim(),
      status: next[1],
      contract_fingerprint: String(item.task_contract?.contract_fingerprint || item.contract_fingerprint || "").trim(),
    };
    const taskStoreKey = String(item.task_contract?.scope?.store_key || item.store_key || selectedStoreKey || "").trim();
    if (taskStoreKey) taskPayload.store_key = taskStoreKey;
    if (note) taskPayload.note = note;
    try {
      await bridgeFetch("/tasks/update", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
        body: JSON.stringify(taskPayload),
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

function materialGovernanceScope() {
  return {
    store_key: String(currentQianchuanCatalog?.selected_store_key || selectedStoreKey || ""),
    account_key: String(currentQianchuanCatalog?.selected_account_key || selectedQianchuanAccount || ""),
  };
}

function renderMaterialGovernance(creative = currentCreativeAnalysis || {}) {
  const policy = globalThis.DianMaterialGovernance;
  if (!policy || !document.getElementById("material-governance-status")) return;
  const scope = materialGovernanceScope();
  const scopeKey = policy.fingerprint(scope);
  const preview = policy.deriveGovernance({
    creative,
    scope,
    appliedPackage: currentMaterialGovernancePackages[scopeKey] || null,
  });
  currentMaterialGovernancePreview = preview;
  const status = document.getElementById("material-governance-status");
  status.className = preview.already_applied ? "safe" : preview.blockers.length ? "danger" : "";
  status.textContent = preview.already_applied
    ? `本机 v${preview.package.revision}`
    : preview.can_apply ? `${preview.changed_count} 项待保存` : "等待准备";
  document.getElementById("material-governance-protect").textContent = `${preview.protected_count || 0} 条`;
  document.getElementById("material-governance-retire").textContent = `${preview.retire_candidate_count || 0} 条`;
  document.getElementById("material-governance-retest").textContent = `${preview.retest_count || 0} 条`;
  document.getElementById("material-governance-version").textContent = `v${preview.package.revision || 1} · 本机`;
  const blockers = document.getElementById("material-governance-blockers");
  if (preview.blockers.length) {
    blockers.className = "material-governance-blockers danger";
    blockers.textContent = preview.blockers.join("；");
  } else {
    blockers.className = "material-governance-blockers safe";
    blockers.textContent = `${preview.first_match_notice} 当前覆盖 ${preview.affected_material_count} 条素材，自动删除保持关闭。`;
  }
  const rules = document.getElementById("material-governance-rules");
  if (!preview.classification.rules.length) {
    rules.className = "material-governance-rules empty-state";
    rules.textContent = "等待生成按优先级排列的素材规则。";
  } else {
    rules.className = "material-governance-rules";
    rules.replaceChildren(...preview.classification.rules.map((item) => {
      const card = document.createElement("article"); card.className = "material-governance-rule";
      const priority = document.createElement("b"); priority.textContent = String(item.priority);
      const title = document.createElement("strong"); title.textContent = item.label;
      const count = document.createElement("span"); count.textContent = `命中 ${item.hit_count} 条`;
      const detail = document.createElement("small"); detail.textContent = `${item.description} 动作：${item.action}。`;
      card.append(priority, title, count, detail);
      return card;
    }));
  }
  const previewList = document.getElementById("material-governance-preview");
  const bucketPriority = { retire: 0, protect: 1, retest: 2, test: 3, observe: 4 };
  const items = [...preview.classification.items].sort((a, b) => (bucketPriority[a.bucket] ?? 9) - (bucketPriority[b.bucket] ?? 9)).slice(0, 10);
  if (!items.length) {
    previewList.className = "material-governance-preview empty-state";
    previewList.textContent = "命中规则后展示素材分池预览。";
  } else {
    previewList.className = "material-governance-preview";
    previewList.replaceChildren(...items.map((item) => {
      const row = document.createElement("article"); row.className = `material-governance-item ${item.bucket}`;
      const name = document.createElement("strong"); name.textContent = item.name;
      const bucket = document.createElement("b"); bucket.textContent = item.bucket_label;
      const evidence = document.createElement("small");
      evidence.textContent = `${item.rule_label} · 消耗 ${item.evidence.spend == null ? "--" : `¥${Number(item.evidence.spend).toFixed(2)}`} · ROI ${item.evidence.roi == null ? "--" : Number(item.evidence.roi).toFixed(2)} · 订单 ${item.evidence.orders == null ? "--" : item.evidence.orders}`;
      row.append(name, bucket, evidence);
      return row;
    }));
  }
  document.getElementById("material-governance-notice").textContent = preview.notice;
  const save = document.getElementById("material-governance-save");
  save.disabled = preview.can_apply !== true;
  save.textContent = preview.overwrite_warning ? "确认更新本机治理包" : preview.already_applied ? "素材治理包已是最新" : "保存素材治理包到本机";
}

function materialMetricValue(value, { prefix = "", suffix = "", digits = 2 } = {}) {
  if (value === null || value === undefined || value === "" || !Number.isFinite(Number(value))) return "--";
  const rendered = Number(value).toFixed(digits).replace(/\.00$/, "").replace(/(\.\d)0$/, "$1");
  return `${prefix}${rendered}${suffix}`;
}

function materialCard(item = {}) {
  const evidence = item.evidence || {};
  const card = document.createElement("article");
  card.className = `material-card ${item.level || "info"}`;
  const heading = document.createElement("header");
  const copy = document.createElement("div");
  const title = document.createElement("strong");
  title.textContent = item.name || "未命名素材";
  title.title = title.textContent;
  const detail = document.createElement("small");
  detail.textContent = item.funnel_label || evidence.assessment || "素材表现待补齐";
  copy.append(title, detail);
  const status = document.createElement("b");
  status.className = "material-card-status";
  status.textContent = item.status || "等待数据";
  heading.append(copy, status);

  const metrics = document.createElement("div");
  metrics.className = "material-card-metrics";
  [
    ["消耗", materialMetricValue(evidence.spend, { prefix: "¥" })],
    ["ROI", materialMetricValue(evidence.roi)],
    ["成交", materialMetricValue(evidence.orders, { digits: 0, suffix: " 单" })],
    ["点击率", materialMetricValue(evidence.ctr, { suffix: "%" })],
  ].forEach(([label, value]) => {
    const metric = document.createElement("span");
    const name = document.createElement("small"); name.textContent = label;
    const number = document.createElement("b"); number.textContent = value;
    metric.append(name, number); metrics.append(metric);
  });
  const suggestion = document.createElement("p");
  suggestion.textContent = item.suggestion || "继续积累素材表现数据。";
  card.append(heading, metrics, suggestion);
  return card;
}

function renderCreativeAnalysis(creative = {}) {
  currentCreativeAnalysis = creative && typeof creative === "object" ? creative : {};
  const summary = creative.summary || {};
  const recommendations = creative.recommendations || [];
  const creativeSignals = Number(summary.risky_videos || 0) + Number(summary.untested_videos || 0) + Number(summary.high_potential_videos || 0);
  setModuleActionCount("creative-actions", recommendations.length || creativeSignals);
  document.getElementById("creative-status").textContent = creative.data_status === "ready" ? "分析完成" : "等待数据";
  document.getElementById("creative-count").textContent = `${summary.total_videos || 0} 条`;
  renderMetricStrip("creative-metrics", {
    素材总数: summary.total_videos || 0,
    在投素材: summary.spending_videos || 0,
    待测试: summary.untested_videos || 0,
    风险素材: summary.risky_videos || 0,
  });
  document.getElementById("creative-analysis-method").textContent = creative.analysis_method || "展示 → 点击 → 成交 → ROI";
  renderMaterialGovernance(currentCreativeAnalysis);
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
  if (!videos.length) {
    container.className = "material-card-grid empty-state";
    container.textContent = "暂时没有素材；请先同步巨量千川视频库。";
    return;
  }
  container.className = "material-card-grid";
  container.replaceChildren(...videos.slice(0, 30).map(materialCard));
}

function renderValueLedger(ledger = {}) {
  const summary = ledger.summary || {};
  const milestones = ledger.value_milestones || {};
  const verifiedResults = Number(summary.verified_results ?? milestones.verified_result_count ?? 0);
  const verifiedValue = Boolean(
    summary.first_verified_value_complete
    ?? milestones.first_verified_value_complete
    ?? ledger.first_verified_value_complete
  );
  document.getElementById("value-ledger-status").textContent = !ledger.trusted_scope
    ? "等待首次巡店"
    : verifiedValue
      ? `已验证结果 ${verifiedResults} 项`
      : "首次价值待回读";
  renderMetricStrip("value-ledger-metrics", {
    人工结案任务: summary.manual_closed_tasks ?? summary.completed_tasks ?? 0,
    任务证据结果: summary.evidence_backed_task_results ?? milestones.evidence_backed_task_result_count ?? 0,
    投放已回读: summary.readback_promotion_actions ?? summary.verified_actions ?? 0,
    投放已评估: summary.evaluated_promotion_actions ?? summary.evaluated_actions ?? 0,
    有效率: summary.effective_rate == null ? "--" : `${summary.effective_rate}%`,
    受控预算幅度: `¥${summary.protected_budget_capacity || 0}`,
  });
  const milestoneNote = verifiedValue
    ? "首次已验证价值已完成：至少已有页面回读或新快照支持的任务结果。"
    : "首次价值待回读：看过任务或人工结案，不等于已经产生经营效果。";
  document.getElementById("value-ledger-note").textContent = `${milestoneNote} ${ledger.note || "价值账本只记录已回读、可复核的数据。"}`;
}

function taskModuleTarget(item = {}) {
  const explicitTarget = String(item.task_contract?.navigation?.target_id || "");
  const allowedTargets = new Set([
    "scan-card", "product-operating-graph", "shelf-actions", "live-actions", "promotion-plan-center",
    "creative-actions", "inventory", "automation-section", "today-task-center",
  ]);
  if (allowedTargets.has(explicitTarget)) return explicitTarget;
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
  const workspaceKey = String(item.task_contract?.navigation?.workspace_key || "");
  const targetId = String(item.task_contract?.navigation?.target_id || "");
  if (workspaceKey === "content-operations" || targetId === "creative-actions") return "内容";
  if (["promotion-overview", "live-operations"].includes(workspaceKey)
      || ["promotion-plan-center", "live-actions"].includes(targetId)) return "直播投放";
  if (workspaceKey === "shelf-products"
      || ["product-operating-graph", "shelf-actions", "inventory"].includes(targetId)) return "货架商品";
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

function revealModuleByChildId(targetId, options = {}) {
  const target = targetId ? document.getElementById(targetId) : null;
  const section = target?.closest(".module-section");
  if (!section) return false;
  return navigateToWorkspaceElement(section, {
    role: options.role || section.dataset.owner || "",
    title: options.title || "",
    subtitle: options.subtitle || "",
    focusElement: target,
    highlight: true,
  });
}

function jumpToTaskModule(item, button) {
  if (!revealModuleByChildId(taskModuleTarget(item), {
    role: taskBusinessRole(item),
    title: `${taskBusinessRole(item)} · 任务详情`,
    subtitle: item.title || "查看任务判断依据与下一步动作。",
  })) return;
  button.textContent = "已定位到详情";
  setTimeout(() => {
    button.textContent = "查看对应模块 ↓";
  }, 1800);
}

function observationWindowMinutes(value) {
  const text = String(value || "").trim().toLowerCase();
  if (!text || /(立即|马上|实时)/.test(text)) return 0;
  const values = [...text.matchAll(/(\d+(?:\.\d+)?)(?:\s*[–—-]\s*\d+(?:\.\d+)?)?\s*(分钟|小时|天)/g)].map((match) => {
    const amount = Number(match[1]);
    const multiplier = match[2] === "分钟" ? 1 : match[2] === "小时" ? 60 : 1440;
    return Math.max(0, Math.round(amount * multiplier));
  });
  return values.length ? Math.min(...values) : null;
}

function observationPrimaryAction(item = {}, nowMs = Date.now()) {
  const contract = item.task_contract && typeof item.task_contract === "object" ? item.task_contract : {};
  const completion = contract.completion_contract && typeof contract.completion_contract === "object" ? contract.completion_contract : {};
  const resultStatus = String(contract.result?.status || "pending");
  const explicitDueAt = Number(
    item.observation_due_at_ms
    || item.due_at_ms
    || completion.observation_due_at_ms
    || completion.due_at_ms
    || 0,
  );
  const startAt = Date.parse(item.review_started_at || item.updated_at || "");
  const minutes = observationWindowMinutes(item.observation_window || completion.observation_window);
  const inferredDueAt = Number.isFinite(startAt) && minutes !== null ? startAt + minutes * 60_000 : 0;
  const dueAtMs = explicitDueAt > 0 ? explicitDueAt : inferredDueAt;
  const resultReady = !["", "pending", "observing", "awaiting_readback"].includes(resultStatus);
  const due = resultReady || (dueAtMs > 0 && Number(nowMs) >= dueAtMs);
  if (due) {
    return {
      due: true,
      dueAtMs,
      label: "到期回读",
      detail: resultReady
        ? "观察结果已经生成；先回读同一经营对象，再决定是否结案。"
        : "观察窗口已结束；先同步同一商品或计划的最新数据，不要重复执行原动作。",
    };
  }
  return {
    due: false,
    dueAtMs,
    label: "查看观察进度",
    detail: dueAtMs > 0
      ? `正在观察，不要重复调整。预计 ${new Date(dueAtMs).toLocaleString()} 可以回读。`
      : `正在观察，不要重复调整。按${item.observation_window || completion.observation_window || "一个完整数据周期"}查看进度。`,
  };
}

function taskCard(item, options = {}) {
  const card = document.createElement("article");
  card.className = `task-card ${item.level || "info"}`;
  if (item.id) card.dataset.taskId = item.id;
  const observationAction = item.status === "observing" ? observationPrimaryAction(item) : null;
  const meta = document.createElement("div");
  meta.className = "task-meta";
  const owner = taskBusinessRole(item);
  const queuePrefix = options.queueIndex ? `第 ${options.queueIndex} 项 · ` : "";
  const taskPhaseLabel = observationAction
    ? observationAction.due ? "到期回读" : "等待观察"
    : item.level === "high" ? "立即处理" : item.level === "opportunity" ? "增长机会" : "今日处理";
  meta.textContent = `${queuePrefix}${taskPhaseLabel} · ${owner || "运营"}${item.carried_over ? " · 跨日延续" : ""}`;
  const title = document.createElement("strong"); title.textContent = item.title || "运营任务";
  const assignee = document.createElement("div"); assignee.className = "task-assignee";
  assignee.textContent = `负责人：${item.assignee || item.owner || "待分配"}${item.last_operator ? ` · 最近操作：${item.last_operator}` : ""}`;
  const action = document.createElement("p");
  const actionLabel = document.createElement("b"); actionLabel.textContent = "下一步：";
  action.append(actionLabel, document.createTextNode(observationAction?.detail || item.action || item.suggestion || "请先回到后台核对数据。"));
  const chips = document.createElement("div"); chips.className = "task-chips";
  const taskContract = item.task_contract && Number(item.task_contract.contract_version) === 2 ? item.task_contract : null;
  const eligibility = taskContract?.eligibility || {};
  const resultStatus = taskContract?.result?.status || "pending";
  const resultLabels = {
    pending: "结果待产生", manual_verified: "已关闭未验证", inconclusive: "已回读·证据不足",
    effective: "已验证有效", ineffective: "已验证无效",
  };
  const trustLabel = currentOperationContext?.state === "ready"
    ? "当前数据可信"
    : currentOperationContext?.state === "blocked"
      ? "经营判断已暂停"
      : "建议需人工复核";
  [
    item.impact,
    item.confidence === "high" ? "证据较充分" : "证据需复核",
    taskContract ? (item.status === "observing" ? "观察中·禁止重复执行" : eligibility.can_start ? "任务可开始" : "开始条件未满足") : null,
    taskContract ? resultLabels[resultStatus] || "结果待核对" : null,
    trustLabel,
  ].filter(Boolean).forEach((value) => {
    const chip = document.createElement("span"); chip.textContent = value; chips.append(chip);
  });
  const detail = document.createElement("details"); detail.className = "task-detail";
  const detailSummary = document.createElement("summary"); detailSummary.textContent = "为什么这样建议？";
  const evidence = document.createElement("small"); evidence.textContent = `数据依据：${item.evidence || "当前页面数据"}`;
  const acceptance = document.createElement("small"); acceptance.textContent = `完成后检查：${item.acceptance || "确认后台数据已经更新"}`;
  detail.append(detailSummary, evidence, acceptance);
  if (taskContract) {
    const sourceRef = taskContract.source_refs?.[0] || {};
    const contractMeta = document.createElement("small");
    const captured = sourceRef.captured_at_ms ? new Date(sourceRef.captured_at_ms).toLocaleString() : "等待同步";
    contractMeta.className = "task-contract-summary";
    contractMeta.textContent = `任务协议 v2 · 目标：${taskContract.subject?.name || "当前经营对象"} · 数据时间：${captured} · 回读：${taskContract.completion_contract?.readback_required ? "必须" : "人工"}`;
    detail.append(contractMeta);
    const blockers = Array.isArray(eligibility.blockers) ? eligibility.blockers.map((entry) => entry?.message).filter(Boolean) : [];
    if (blockers.length) {
      const blocker = document.createElement("small");
      blocker.className = "task-contract-blocker";
      blocker.textContent = `开始前补齐：${blockers.join("；")}`;
      detail.append(blocker);
    }
  }
  card.append(meta, title, assignee, action, chips, detail);
  if (item.status !== "observing") appendCopyAction(card, item.action_params);
  if (item.id) {
    const actions = document.createElement("div"); actions.className = "task-actions";
    const statusLabel = document.createElement("span");
    const labels = { todo: "待处理", doing: "进行中", observing: "效果观察", blocked: "已阻止", done: "已完成" };
    statusLabel.textContent = item.status === "observing" && observationAction?.due ? "等待回读" : labels[item.status] || "待处理";
    const transitions = item.status === "todo" ? [["开始处理", "doing"], ["转交", "transfer"], ["阻止", "blocked"]]
      : item.status === "doing" ? [["进入效果观察", "observing"], ["转交", "transfer"], ["阻止", "blocked"]]
      : item.status === "observing" ? [[observationAction.label, "observation_progress"]]
      : item.status === "blocked" ? [["解除阻止", "todo"], ["转交", "transfer"]]
      : [["重新打开", "todo"]];
    actions.append(statusLabel);
    if (item.status === "doing") {
      const loopHint = document.createElement("small");
      loopHint.className = "task-loop-hint";
      loopHint.textContent = "完成操作后先进入效果观察，不能直接结案。";
      actions.append(loopHint);
    }
    transitions.forEach(([label, status]) => {
      const button = document.createElement("button"); button.textContent = label; button.setAttribute("aria-label", `${label}：${item.title || '任务'}`);
      if (status === "observation_progress") {
        button.dataset.observationPrimary = "true";
        button.dataset.observationTaskId = item.id;
      } else {
        button.dataset.taskWrite = "task-status";
        markAgentWriteControl(button);
      }
      if (status === "doing" && item.status === "todo" && taskContract && eligibility.can_start !== true) {
        button.disabled = true;
        button.title = (eligibility.blockers || []).map((entry) => entry?.message).filter(Boolean).join("；") || "任务当前不满足开始条件";
      }
      button.addEventListener("click", async () => {
        button.disabled = true;
        if (status === "observation_progress") {
          try {
            if (observationAction.due) {
              button.textContent = "正在读取最新结果…";
              await loadDashboard();
            } else {
              detail.open = true;
            }
            revealModuleByChildId(taskModuleTarget(item), {
              role: taskBusinessRole(item),
              title: observationAction.due ? "观察到期 · 结果回读" : "效果观察进度",
              subtitle: observationAction.detail,
            });
            button.disabled = false;
            button.textContent = observationAction.label;
          } catch (error) {
            window.alert(error?.message || "观察进度读取失败，请检查本地 Agent 后重试。");
            button.disabled = false;
            button.textContent = observationAction.label;
          }
          return;
        }
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
        if (status === "done") {
          note = window.prompt("请填写结案说明：做了什么、结果怎样、是否需要继续观察？", "")?.trim() || "";
          if (!note) { button.disabled = false; return; }
        }
        try {
          await bridgeFetch("/tasks/update", { method: "POST", headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" }, body: JSON.stringify({
            task_id: item.id,
            status: nextStatus,
            operator: currentRole,
            assignee: nextAssignee,
            note,
            title: item.title,
            owner: item.owner,
            action: item.action || item.suggestion || "",
            acceptance: item.acceptance || "",
            evidence: item.evidence || "",
            observation_window: item.observation_window || "",
            level: item.level || "",
            contract_version: taskContract?.contract_version || 1,
            contract_fingerprint: taskContract?.contract_fingerprint || "",
            store_key: selectedStoreKey,
          }) });
          await loadDashboard();
        } catch (error) {
          window.alert(error?.message || "任务状态更新失败，请检查本地 Agent 后重试。");
          button.disabled = false;
        }
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
    [fbUp, fbDown, fbDefer].forEach((button) => {
      button.dataset.taskWrite = "feedback";
      markAgentWriteControl(button);
    });
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
    if (item.status !== "observing") card.append(feedback);
  }
  if (options.showModuleLink && item.status !== "observing" && taskModuleTarget(item)) {
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
  if (!items.length) {
    if (id === "manager-tasks" && globalThis.DianSimpleExperience) {
      const state = globalThis.DianSimpleExperience.deriveEmptyState({
        task_count: 0,
        data_state: currentJourneyCommand?.dataState || currentSimpleJourney?.data_state,
        failed: currentDashboardFailures.includes("今日任务"),
        clean: currentOps?.clean === true || currentOps?.diagnostics?.status === "clean",
      });
      container.dataset.emptyState = state.id;
      return empty(container, `${state.title}。${state.detail}`);
    }
    return empty(container, "当前没有需要处理的任务；如果数据未同步，请先完成一次巡检。");
  }
  delete container.dataset.emptyState;
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
    .filter((item) => item.status !== "done" && (opportunity ? item.level === "opportunity" : item.level !== "opportunity"))
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

function renderNextBestAction(items = [], ops = currentOps || {}) {
  const panel = document.getElementById("next-best-action");
  const title = document.getElementById("next-best-action-title");
  const detail = document.getElementById("next-best-action-detail");
  const button = document.getElementById("next-best-action-button");
  const next = items.find((item) => item.status === "doing")
    || items.find((item) => item.status === "observing" && observationPrimaryAction(item).due)
    || items.find((item) => !item.status || item.status === "todo")
    || items.find((item) => item.status === "observing")
    || items[0];
  panel.classList.toggle("empty", !next);
  panel.classList.toggle("urgent", next?.level === "high");
  if (!next) {
    const state = globalThis.DianSimpleExperience.deriveEmptyState({
      task_count: 0,
      data_state: currentJourneyCommand?.dataState || currentSimpleJourney?.data_state,
      failed: currentDashboardFailures.includes("今日任务"),
      clean: ops.clean === true || ops.diagnostics?.status === "clean",
    });
    panel.dataset.emptyState = state.id;
    panel.classList.toggle("attention", ["stale", "failed", "never"].includes(state.id));
    title.textContent = state.title;
    detail.textContent = state.detail;
    const actionCopy = {
      loading: "正在检查",
      failed: "重试失败页面",
      stale: "刷新核心数据",
      never: "开始首次同步",
      clean: "查看检查结果",
      no_actionable_task: "查看判断依据",
    };
    button.textContent = actionCopy[state.id] || "查看当前状态";
    button.disabled = state.id === "loading";
    button.dataset.emptyAction = state.action || "none";
    button.dataset.target = ["failed", "stale", "never"].includes(state.id) ? "scan-card" : "scan-receipt-card";
    button.dataset.observationTaskId = "";
    return;
  }
  delete panel.dataset.emptyState;
  panel.classList.remove("attention");
  button.disabled = false;
  button.dataset.emptyAction = "";
  const observationAction = next.status === "observing" ? observationPrimaryAction(next) : null;
  title.textContent = next.title || "处理当前最高优先级任务";
  detail.textContent = observationAction?.detail || next.action || next.acceptance || next.evidence || "打开处置队列查看依据和验收标准。";
  button.textContent = observationAction?.label || (next.status === "doing" ? "继续处理" : "开始处理");
  button.dataset.target = "manager-tasks";
  button.dataset.observationTaskId = observationAction ? next.id || "" : "";
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

function livePacingNumber(value, digits = 1) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  return number.toLocaleString("zh-CN", { minimumFractionDigits: 0, maximumFractionDigits: digits });
}

function renderLivePacing(pacing = {}) {
  const status = document.getElementById("live-pacing-status");
  if (!status) return;
  const decision = pacing.decision && typeof pacing.decision === "object" ? pacing.decision : {};
  const interval = pacing.latest_interval && typeof pacing.latest_interval === "object" ? pacing.latest_interval : {};
  const latest = pacing.latest_values && typeof pacing.latest_values === "object" ? pacing.latest_values : {};
  const level = ["high", "warning", "safe"].includes(decision.level) ? decision.level : "info";
  status.textContent = pacing.status_label || "等待同场次样本";
  status.className = level;

  const spendRate = livePacingNumber(interval.spend_per_hour, 1);
  const orderRate = livePacingNumber(interval.orders_per_hour, 1);
  const roi = livePacingNumber(interval.interval_roi ?? latest.roi, 2);
  document.getElementById("live-pacing-spend-rate").textContent = spendRate === null ? "待积累" : `¥${spendRate}/小时`;
  document.getElementById("live-pacing-order-rate").textContent = orderRate === null ? "待积累" : `${orderRate} 单/小时`;
  document.getElementById("live-pacing-roi").textContent = roi === null ? "待积累" : roi;
  document.getElementById("live-pacing-age").textContent = pacing.age_label || "等待同步";

  const decisionCard = document.getElementById("live-pacing-decision");
  decisionCard.className = `live-pacing-decision ${level}`;
  document.getElementById("live-pacing-decision-title").textContent = decision.title || "先积累直播速度样本";
  document.getElementById("live-pacing-decision-action").textContent = decision.action || "保持当前设置，取得至少两个同场次快照后再判断。";
  document.getElementById("live-pacing-decision-evidence").textContent = decision.evidence || "缺失值不会按 0 处理。";
  document.getElementById("live-pacing-pace").textContent = pacing.pace_label || "速度待积累";

  const blockers = Array.isArray(pacing.blockers) ? pacing.blockers.filter(Boolean) : [];
  const blockersNode = document.getElementById("live-pacing-blockers");
  blockersNode.className = `live-pacing-blockers${blockers.length ? "" : " safe"}`;
  blockersNode.replaceChildren(...(blockers.length ? blockers : ["判断门槛已齐，仍只生成只读节奏建议。"]).map((text) => {
    const badge = document.createElement("span");
    badge.textContent = text;
    return badge;
  }));

  const intervals = Array.isArray(pacing.recent_intervals) ? pacing.recent_intervals.slice(-3).reverse() : [];
  const intervalNode = document.getElementById("live-pacing-intervals");
  if (!intervals.length) {
    intervalNode.className = "live-pacing-intervals empty-state";
    intervalNode.textContent = "尚无可比较的增量窗口。";
  } else {
    intervalNode.className = "live-pacing-intervals";
    intervalNode.replaceChildren(...intervals.map((item, index) => {
      const card = document.createElement("article");
      card.className = "live-pacing-interval";
      const header = document.createElement("header");
      const title = document.createElement("b");
      title.textContent = `${item.start_label || "--:--"} → ${item.end_label || "--:--"}${index === 0 ? " · 最近" : ""}`;
      const duration = document.createElement("span");
      duration.textContent = item.duration_minutes == null ? "窗口未知" : `${livePacingNumber(item.duration_minutes, 1)} 分钟`;
      header.append(title, duration);
      const list = document.createElement("dl");
      const entries = [
        ["消耗增量", item.spend_delta == null ? "--" : `+¥${livePacingNumber(item.spend_delta, 1)}`],
        ["成交增量", item.orders_delta == null ? "--" : `+${livePacingNumber(item.orders_delta, 1)} 单`],
        ["GMV 增量", item.gmv_delta == null ? "--" : `+¥${livePacingNumber(item.gmv_delta, 1)}`],
        ["窗口 ROI", item.interval_roi == null ? "--" : livePacingNumber(item.interval_roi, 2)],
      ];
      entries.forEach(([label, value]) => {
        const row = document.createElement("div");
        const term = document.createElement("dt"); term.textContent = label;
        const detail = document.createElement("dd"); detail.textContent = value;
        row.append(term, detail); list.append(row);
      });
      card.append(header, list);
      return card;
    }));
  }

  const sourceBits = [pacing.source_label, pacing.window_label, `${pacing.session_point_count || 0} 个同场快照`].filter(Boolean);
  if (pacing.session_reset_detected) sourceBits.push("已识别场次重置");
  sourceBits.push("只读，不自动修改预算、时长或状态");
  document.getElementById("live-pacing-source").textContent = sourceBits.join(" · ");
}

function productGraphMetric(product = {}, metric = "") {
  const candidates = Object.values(product.metrics || {})
    .map((values) => values?.[metric])
    .filter((value) => value != null && Number.isFinite(Number(value)));
  return candidates.length ? Number(candidates[0]) : null;
}

function formatProductGraphMetric(value, options = {}) {
  if (value == null || !Number.isFinite(Number(value))) return "--";
  const numeric = Number(value);
  const rendered = numeric.toFixed(options.decimals ?? 2).replace(/\.00$/, "").replace(/(\.\d)0$/, "$1");
  return `${options.prefix || ""}${rendered}${options.suffix || ""}`;
}

function renderProductGraphReadiness(readiness = {}, products = []) {
  const container = document.getElementById("product-graph-readiness");
  const steps = Array.isArray(readiness.steps) ? readiness.steps : [];
  const stateLabels = {
    ready: "已完成",
    optional: "可稍后",
    stale: "已过期",
    recapture_required: "需重采",
    identity_missing: "信息不全",
    metric_missing: "缺字段",
    mapping_missing: "缺映射",
    missing: "未采集",
    blocked: "待确认",
  };
  container.replaceChildren(...steps.map((step, index) => {
    const item = document.createElement("article");
    item.className = `product-graph-readiness-step ${step.status || "missing"}`;
    const marker = document.createElement("b");
    marker.textContent = step.status === "ready" ? "✓" : String(index + 1);
    const copy = document.createElement("div");
    const title = document.createElement("strong"); title.textContent = step.label || `步骤 ${index + 1}`;
    const detail = document.createElement("small"); detail.textContent = step.detail || "等待检查";
    copy.append(title, detail);
    const status = document.createElement("span"); status.textContent = stateLabels[step.status] || "待补齐";
    item.append(marker, copy, status);
    return item;
  }));
  container.hidden = true;

  const action = readiness.next_action || {};
  const button = document.getElementById("product-graph-collect");
  const secondaryButton = document.querySelector(".product-graph-footer-actions button:not(#product-graph-collect)");
  const baseReady = readiness.status === "base_ready" && products.length > 0;
  const actionKind = "targeted_scan";
  button.textContent = fullScanRunning ? "正在同步商品…" : "同步商品";
  button.disabled = fullScanRunning;
  button.title = action.detail || (baseReady ? "刷新商品卡所需数据" : "同步商品卡所需页面");
  button.dataset.actionKind = actionKind;
  button.dataset.targetId = "";
  button.dataset.pageIds = JSON.stringify(Array.isArray(action.page_ids) ? action.page_ids : ["products", "inventory", "shelf"]);
  if (secondaryButton) {
    secondaryButton.textContent = baseReady ? "进入直播与投放" : "查看货架漏斗详情";
    secondaryButton.dataset.workspaceTarget = baseReady ? "promotion-plan-center" : "shelf-business-center";
    secondaryButton.dataset.workspaceKey = baseReady ? "promotion-overview" : "shelf-funnel";
    secondaryButton.title = baseReady ? "需要投放时再进入，不影响当前商品与库存诊断" : "查看曝光、点击和成交漏斗";
  }
}

function renderProductOperatingGraph(graph = {}) {
  const summary = graph.summary || {};
  const products = Array.isArray(graph.products) ? graph.products : [];
  const readiness = graph.collection_readiness || {};
  const readinessLabels = {
    store_required: "打开抖店后同步商品",
    recapture_required: "需要刷新数据",
    product_data_required: "需要补商品数据",
    base_ready: "基础经营链可用",
    ads_mapping_required: "待补千川映射",
    ready: "单品经营链已建立",
  };
  document.getElementById("product-graph-status").textContent = readinessLabels[readiness.status] || graph.status_label || "等待商品数据";
  renderMetricStrip("product-graph-metrics", {
    已识别商品: summary.products || 0,
    跨渠道关联: summary.cross_channel_products || 0,
    今日需处理: summary.actionable_products || 0,
    禁止直接放量: summary.blocked_from_scaling || 0,
    经营记忆: `${summary.memory_days || 0} 天`,
    信息不全: summary.unresolved_rows || 0,
  });
  renderProductGraphReadiness(readiness, products);
  setModuleActionCount("product-graph-list", summary.actionable_products || 0);
  const container = document.getElementById("product-graph-list");
  if (!products.length) {
    const unresolved = Number(summary.unresolved_rows || 0);
    const nextAction = readiness.next_action || {};
    empty(container, unresolved
      ? `已发现 ${unresolved} 行商品数据，但缺少平台商品 ID、SKU ID 或商家编码；系统没有按同名商品强行关联。`
      : nextAction.detail || "同步商品、库存和千川计划页后，这里会按同一商品定位经营瓶颈，并只给一个下一步。");
    document.getElementById("product-graph-note").textContent = nextAction.detail || readiness.policy
      || (graph.blockers || []).map((item) => item.message).filter(Boolean).join("；")
      || "跨渠道归因必须先取得稳定商品身份；缺少映射时只提示补数据。";
    return;
  }
  container.className = "product-graph-list";
  container.replaceChildren(...products.slice(0, 6).map((product, index) => {
    const decision = product.decision || {};
    const gate = product.promotion_gate || {};
    const card = document.createElement("article");
    card.className = `product-graph-card ${decision.level || "info"}`;
    if (index === 0) {
      card.id = "product-graph-first-recommendation";
      card.tabIndex = -1;
    }
    const heading = document.createElement("header");
    const titleBox = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = product.product_name || "未命名商品";
    const identity = document.createElement("small");
    const identifier = product.product_id || product.sku_id || product.merchant_code || "身份待补齐";
    identity.textContent = `${product.product_id ? "商品 ID" : product.sku_id ? "SKU ID" : product.merchant_code ? "商家编码" : "商品身份"} · ${identifier}`;
    titleBox.append(title, identity);
    const level = document.createElement("b");
    level.textContent = decision.level === "high" ? "立即处理" : decision.level === "opportunity" ? "放量候选" : decision.level === "warning" ? "需要补齐" : "继续观察";
    heading.append(titleBox, level);

    const metrics = document.createElement("div");
    metrics.className = "product-graph-inline-metrics";
    const metricValues = [
      ["ROI", formatProductGraphMetric(productGraphMetric(product, "roi"))],
      ["消耗", formatProductGraphMetric(productGraphMetric(product, "spend"), { prefix: "¥" })],
      ["订单", formatProductGraphMetric(productGraphMetric(product, "orders"), { decimals: 0 })],
      ["库存", formatProductGraphMetric(productGraphMetric(product, "stock"), { decimals: 0 })],
      ["退款率", formatProductGraphMetric(productGraphMetric(product, "refund_rate"), { suffix: "%" })],
    ];
    metricValues.forEach(([label, value]) => {
      const item = document.createElement("span");
      const name = document.createElement("small"); name.textContent = label;
      const metric = document.createElement("b"); metric.textContent = value;
      item.append(name, metric); metrics.append(item);
    });

    const next = document.createElement("section");
    next.className = "product-card-decision";
    const nextLabel = document.createElement("small"); nextLabel.textContent = "下一步";
    const judgmentTitle = document.createElement("strong"); judgmentTitle.textContent = decision.label || "等待商品判断";
    const action = document.createElement("p"); action.textContent = decision.action || "先补齐商品数据。";
    next.append(nextLabel, judgmentTitle, action);

    const details = document.createElement("details");
    details.className = "product-card-details";
    const detailsSummary = document.createElement("summary");
    detailsSummary.textContent = "查看判断依据与安全状态";
    const channels = document.createElement("div");
    channels.className = "product-graph-channels";
    (product.channel_labels || []).forEach((label) => {
      const chip = document.createElement("span"); chip.textContent = label; channels.append(chip);
    });
    const evidence = document.createElement("p");
    evidence.textContent = decision.evidence || "等待同一商品的可比数据";
    const acceptance = document.createElement("small");
    acceptance.textContent = `完成标准：${decision.acceptance || "重新同步并核对同一商品"}`;

    const gateBox = document.createElement("footer");
    gateBox.className = gate.scale_allowed ? "product-graph-gate ready" : "product-graph-gate blocked";
    const gateTitle = document.createElement("strong");
    gateTitle.textContent = gate.scale_allowed ? "可进入小步放量人工复核" : "当前禁止直接放量";
    const gateReason = document.createElement("span");
    gateReason.textContent = gate.scale_allowed ? "提交前仍需逐次授权并在执行后回读" : (gate.reasons || []).slice(0, 3).join(" · ") || "经营证据尚未补齐";
    gateBox.append(gateTitle, gateReason);

    const latest = (product.evidence || [])[0];
    const provenance = document.createElement("small");
    provenance.className = "product-graph-provenance";
    provenance.textContent = latest
      ? `最新证据：${latest.source}/${latest.page_type} · ${latest.captured_at_ms ? new Date(latest.captured_at_ms).toLocaleString() : "时间待确认"} · 质量 ${latest.quality_score || 0}`
      : "尚无可复核证据";
    details.append(detailsSummary, channels, evidence, acceptance, gateBox, provenance);
    card.append(heading, metrics, next, details);
    return card;
  }));
  const unresolved = Number(summary.unresolved_rows || 0);
  const conflict = Number(summary.same_name_conflicts || 0);
  document.getElementById("product-graph-note").textContent = readiness.status === "base_ready"
    ? "商品经营已可用；千川未连接不影响当前商品与库存诊断，可先处理第 1 条单品建议。"
    : unresolved || conflict
      ? `还有 ${unresolved} 行缺少稳定身份，${conflict} 组同名不同商品已保持分离；这些数据不会参与跨渠道因果判断。`
      : "所有关联均来自稳定商品身份；名称只用于展示，不参与自动合并。";
}

function renderOperations(ops, productGraph, shelf, live, creative, coverage = []) {
  currentOps = ops;
  currentOperationsContext = { ops, productGraph, shelf, live, creative, coverage };
  const allTasks = roleTasks(ops, false);
  const allGrowth = roleTasks(ops, true);
  const visibleTasks = managerQueueExpanded ? allTasks : allTasks.slice(0, 3);
  const expand = document.getElementById("manager-expand");
  document.getElementById("task-heading").textContent = "全店 · 今日处置队列";
  document.getElementById("manager-queue-caption").textContent = "跨商品、直播投放和素材统一排序；完成动作后再进入观察。";
  document.getElementById("manager-count").textContent = `${allTasks.length} 项待处理`;
  expand.hidden = allTasks.length <= 3;
  expand.textContent = managerQueueExpanded ? "收起队列" : `查看全部 ${allTasks.length} 项`;
  renderQueueStats(allTasks);
  renderNextBestAction(allTasks, ops);
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
  renderProductOperatingGraph(productGraph || {});
  document.getElementById("shelf-status").textContent = shelf.data_status === "ready" ? "分析完成" : "等待数据";
  renderMetricStrip("shelf-metrics", { 曝光: shelf.funnel?.exposure, 点击: shelf.funnel?.clicks, 成交人数: shelf.funnel?.buyers, 点击率: shelf.funnel?.click_rate == null ? null : `${shelf.funnel.click_rate.toFixed(1)}%` });
  const shelfRecommendations = shelf.recommendations || [];
  setModuleActionCount("shelf-actions", shelfRecommendations.length);
  renderTasks("shelf-actions", shelfRecommendations);
  document.getElementById("live-status").textContent = live.data_status === "ready" ? "分析完成" : "等待数据";
  renderMetricStrip("live-metrics", { 进房: live.funnel?.views, 进房率: live.funnel?.enter_rate == null ? null : `${live.funnel.enter_rate.toFixed(1)}%`, 商品点击: live.funnel?.product_clicks, 订单: live.funnel?.orders, ROI: live.funnel?.roi });
  renderLivePacing(live.pacing || {});
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
  const refreshAvailable = payload.refresh_available === true;
  document.getElementById("sync-oceanengine-data").disabled = !(payload.connected || refreshAvailable);
  if (payload.app_id) appId.value = payload.app_id;
  secret.value = "";
  const secretStorageLabel = payload.secret_storage === "macos_keychain"
    ? "已保存在 macOS 钥匙串；留空继续使用"
    : payload.secret_storage === "windows_dpapi"
      ? "已用 Windows 本机加密保存；留空继续使用"
      : "已由本机安全环境提供；留空继续使用";
  secret.placeholder = payload.secret_saved
    ? secretStorageLabel
    : "从开放平台复制；仅加密保存在本机";
  state.className = payload.connected ? "connected" : refreshAvailable || payload.authorization_in_progress ? "waiting" : "";
  state.textContent = payload.connected
    ? "已连接"
    : refreshAvailable
      ? "授权可自动续期"
    : payload.authorization_in_progress
      ? "等待授权完成"
      : payload.secret_saved
        ? "待授权账号"
        : "尚未连接";
  const connectionAuth = document.getElementById("connection-center-auth");
  if (connectionAuth) {
    connectionAuth.textContent = payload.connected
      ? `官方 API · ${Number(payload.account_count || 0)} 个账号`
      : refreshAvailable
        ? "官方 API · 授权待自动续期"
      : payload.authorization_in_progress
        ? "等待官方授权"
        : "浏览器只读";
  }
  document.getElementById("oceanengine-account-count").textContent = `${Number(payload.account_count || 0)} 个账号`;
  accountBox.replaceChildren(...accounts.map((account) => {
    const chip = document.createElement("span");
    chip.textContent = account.account_name || `千川账号 ${account.account_hint || ""}`;
    return chip;
  }));
  accountBox.hidden = !accounts.length;
  if (payload.connected) {
    result.textContent = accounts.length
      ? `官方 API 已连接，已识别 ${accounts.length} 个授权账号。`
      : "官方 API 已连接；账号名称会在首次 API 同步后补齐。";
    result.className = "ok";
  } else if (refreshAvailable) {
    result.textContent = "Access Token 已过期，但刷新授权仍有效；点击“同步官方数据”会先在本机自动续期，无需重新授权。";
    result.className = "warn";
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
  if (payload.ok === false || payload.recovery_required === true || payload.corruption_detected === true) {
    const detail = String(payload.error?.message || payload.message || "保存的官方同步记录无法验证");
    result.textContent = `官方同步记录异常：${detail} 请重新执行一次只读同步恢复；已有浏览器快照不会丢失。`;
    result.className = "error";
    return;
  }
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

function oceanEngineCenterNode(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function renderOceanEngineSelection() {
  if (!currentOceanEngineAccountCenter) return;
  const action = document.getElementById("oauth-center-batch-action").value;
  const summary = DianOceanEngineAccountCenter.selectionSummary(
    currentOceanEngineAccountCenter.accounts,
    [...oceanEngineAccountSelection],
    action,
  );
  document.getElementById("oauth-center-selection").textContent = summary.account_count
    ? `已选 ${summary.account_count} 个账户 · ${summary.advertiser_count} 个广告账户`
    : "尚未选择账户";
  document.getElementById("oauth-center-preview").disabled = !summary.can_preview || !currentOceanEngineAccountCenter.safe;
}

function oceanEngineAccountCard(account) {
  const card = oceanEngineCenterNode("article", "oauth-center-account");
  card.dataset.accountKey = account.account_key;
  card.dataset.state = account.state;

  const heading = oceanEngineCenterNode("header", "oauth-account-heading");
  const select = oceanEngineCenterNode("label", "oauth-account-select");
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.dataset.oauthAccountSelect = account.account_key;
  checkbox.checked = oceanEngineAccountSelection.has(account.account_key);
  checkbox.setAttribute("aria-label", `选择 ${account.display_name}`);
  const title = oceanEngineCenterNode("span", "oauth-account-title");
  title.append(
    oceanEngineCenterNode("strong", "", account.display_name),
    oceanEngineCenterNode("small", "", `${account.masked_id || "本机匿名身份"}${account.platform_name && account.platform_name !== account.display_name ? ` · 平台名：${account.platform_name}` : ""}`),
  );
  select.append(checkbox, title);
  heading.append(select, oceanEngineCenterNode("span", "oauth-account-state", account.state_label));

  const meta = oceanEngineCenterNode("div", "oauth-account-meta");
  const storeLabel = account.linked_stores.length
    ? account.linked_stores.map((store) => store.label).join(" / ")
    : "尚未绑定店铺";
  [
    `分组：${account.preferences.group_name}`,
    `店铺：${storeLabel}`,
    `${account.advertiser_count} 个广告账户`,
    account.preferences.managed ? "已开启本地托管" : "未开启托管",
  ].forEach((value) => meta.append(oceanEngineCenterNode("span", "", value)));

  const health = oceanEngineCenterNode("div", "oauth-account-health");
  [
    ["授权状态", account.token.label],
    ["数据状态", account.sync.freshness.label],
    ["接口健康", `${account.sync.success_count}/${account.sync.endpoint_count} 成功${account.sync.failure_count ? ` · ${account.sync.failure_count} 失败` : ""}`],
  ].forEach(([label, value]) => {
    const cell = document.createElement("div");
    cell.append(oceanEngineCenterNode("small", "", label), oceanEngineCenterNode("strong", "", value));
    health.append(cell);
  });

  const capabilities = oceanEngineCenterNode("div", "oauth-account-capabilities");
  account.capabilities.forEach((item) => {
    const chip = oceanEngineCenterNode("span", item.state, `${item.verified ? "✓" : item.state === "partial" ? "△" : "×"} ${item.label}`);
    chip.title = item.status_label;
    capabilities.append(chip);
  });

  const settings = oceanEngineCenterNode("details", "oauth-account-settings");
  settings.append(oceanEngineCenterNode("summary", "", "账户别名、分组与托管设置"));
  const controls = oceanEngineCenterNode("div", "oauth-account-setting");
  const aliasLabel = document.createElement("label");
  aliasLabel.append("账户别名");
  const aliasInput = document.createElement("input");
  aliasInput.dataset.oauthAccountAlias = account.account_key;
  aliasInput.value = account.preferences.alias;
  aliasInput.placeholder = account.platform_name || account.display_name;
  aliasInput.maxLength = 40;
  aliasLabel.append(aliasInput);
  const groupLabel = document.createElement("label");
  groupLabel.append("业务分组");
  const groupInput = document.createElement("input");
  groupInput.dataset.oauthAccountGroup = account.account_key;
  groupInput.value = account.preferences.group_name;
  groupInput.maxLength = 24;
  groupLabel.append(groupInput);
  const toggles = oceanEngineCenterNode("div", "oauth-account-toggles");
  const syncLabel = document.createElement("label");
  const syncToggle = document.createElement("input");
  syncToggle.type = "checkbox";
  syncToggle.dataset.oauthAccountSync = account.account_key;
  syncToggle.checked = account.preferences.sync_enabled;
  syncLabel.append(syncToggle, "允许只读同步");
  const managedLabel = document.createElement("label");
  const managedToggle = document.createElement("input");
  managedToggle.type = "checkbox";
  managedToggle.dataset.oauthAccountManaged = account.account_key;
  managedToggle.checked = account.preferences.managed;
  managedToggle.disabled = !account.preferences.sync_enabled;
  managedLabel.append(managedToggle, "纳入本地托管");
  toggles.append(syncLabel, managedLabel);
  const save = oceanEngineCenterNode("button", "oauth-account-save", "保存账户设置");
  save.type = "button";
  save.dataset.oauthAccountSave = account.account_key;
  controls.append(aliasLabel, groupLabel, toggles, save);
  settings.append(controls, oceanEngineCenterNode("p", "oauth-account-note", `下一步：${account.next_action || "同步并检查账户能力"}`));

  card.append(heading, meta, health, capabilities, settings);
  return card;
}

function renderOceanEngineAccountCards() {
  if (!currentOceanEngineAccountCenter) return;
  const list = document.getElementById("oauth-center-list");
  const visible = DianOceanEngineAccountCenter.filterAccounts(
    currentOceanEngineAccountCenter.accounts,
    {
      query: document.getElementById("oauth-center-search").value,
      status: document.getElementById("oauth-center-status-filter").value,
      group: document.getElementById("oauth-center-group-filter").value,
    },
  );
  if (!visible.length) {
    const message = currentOceanEngineAccountCenter.accounts.length
      ? "没有符合当前筛选条件的账户。"
      : "完成官方授权后，这里会出现账户能力与同步状态。";
    list.replaceChildren(oceanEngineCenterNode("p", "oauth-center-empty", message));
  } else {
    list.replaceChildren(...visible.map(oceanEngineAccountCard));
  }
  renderOceanEngineSelection();
}

function renderOceanEngineAccountCenter(payload = {}) {
  const fallback = {
    accounts: [], platform_write_enabled: false, automatic_batch_submit: false, secrets_exposed: false,
    notice: "账户中心暂时无法读取，请确认本地 Agent 已更新。",
  };
  currentOceanEngineAccountCenter = DianOceanEngineAccountCenter.normalize(payload || fallback);
  const knownKeys = new Set(currentOceanEngineAccountCenter.accounts.map((item) => item.account_key));
  oceanEngineAccountSelection = new Set([...oceanEngineAccountSelection].filter((key) => knownKeys.has(key)));
  const summary = currentOceanEngineAccountCenter.summary;
  document.getElementById("oauth-center-total").textContent = summary.total;
  document.getElementById("oauth-center-managed").textContent = summary.managed;
  document.getElementById("oauth-center-attention").textContent = summary.attention;
  document.getElementById("oauth-center-advertisers").textContent = summary.advertisers;
  const safety = document.getElementById("oauth-center-safety");
  safety.textContent = currentOceanEngineAccountCenter.safe ? "真实投放写入：关闭" : "安全拦截已触发";
  safety.title = currentOceanEngineAccountCenter.notice;

  const groupFilter = document.getElementById("oauth-center-group-filter");
  const previousGroup = groupFilter.value || "all";
  const allOption = document.createElement("option");
  allOption.value = "all";
  allOption.textContent = "全部分组";
  const options = currentOceanEngineAccountCenter.groups.map((group) => {
    const option = document.createElement("option");
    option.value = group;
    option.textContent = group;
    return option;
  });
  groupFilter.replaceChildren(allOption, ...options);
  groupFilter.value = currentOceanEngineAccountCenter.groups.includes(previousGroup) ? previousGroup : "all";
  renderOceanEngineAccountCards();
}

function renderOceanEngineBatchPreview(preview = {}) {
  const result = document.getElementById("oauth-center-preview-result");
  const runSync = document.getElementById("oauth-center-run-sync");
  if (preview.platform_write_enabled === true || preview.automatic_submit === true) {
    currentOceanEngineBatchPreview = null;
    runSync.hidden = true;
    runSync.disabled = true;
    result.textContent = "安全拦截：批量预演返回了不允许的真实写入标记。";
    result.className = "oauth-center-preview-result error";
    return;
  }
  currentOceanEngineBatchPreview = preview;
  const blockedExample = Array.isArray(preview.blocked) && preview.blocked.length
    ? `；阻止原因：${(preview.blocked[0].reasons || []).join("、")}`
    : "";
  result.textContent = `${preview.action_label || "批量动作"}：${Number(preview.eligible_count || 0)} 个账户可进入下一步，${Number(preview.blocked_count || 0)} 个被阻止，涉及 ${Number(preview.advertiser_count || 0)} 个广告账户${blockedExample}。${preview.notice || ""}`;
  result.className = `oauth-center-preview-result ${Number(preview.blocked_count || 0) ? "warn" : "ok"}`;
  runSync.hidden = preview.action !== "sync" || Number(preview.eligible_count || 0) <= 0;
  runSync.disabled = runSync.hidden;
}

function resetOceanEngineBatchPreview(message = "选择账户后先预演影响；投放动作不会直接提交到平台。") {
  currentOceanEngineBatchPreview = null;
  const runSync = document.getElementById("oauth-center-run-sync");
  runSync.hidden = true;
  runSync.disabled = true;
  const result = document.getElementById("oauth-center-preview-result");
  result.textContent = message;
  result.className = "oauth-center-preview-result";
}

function stopOceanEngineStatusPolling(expectedEpoch = oceanengineStatusPollEpoch, expectedPoller = oceanengineStatusPoller) {
  if (
    !expectedPoller
    || expectedEpoch !== oceanengineStatusPollEpoch
    || oceanengineStatusPoller !== expectedPoller
  ) return false;
  clearInterval(expectedPoller);
  oceanengineStatusPoller = null;
  return true;
}

async function refreshOceanEngineStatus() {
  const generation = ++oceanengineStatusRefreshGeneration;
  const pollingIdentity = {
    epoch: oceanengineStatusPollEpoch,
    poller: oceanengineStatusPoller,
  };
  const [status, sync, accountCenter] = await Promise.all([
    bridgeFetch("/oauth/oceanengine/status"),
    bridgeFetch("/oauth/oceanengine/sync-status"),
    bridgeFetch("/oauth/oceanengine/account-center").catch(() => ({
      accounts: [], platform_write_enabled: false, automatic_batch_submit: false,
      notice: "账户中心暂时无法读取，请确认本地 Agent 已更新。",
    })),
  ]);
  // A slower request must not repaint authorization state after a newer
  // manual refresh or polling request has already completed.
  if (generation !== oceanengineStatusRefreshGeneration) return { ...status, stale: true };
  renderOceanEngineOAuth(status);
  renderOceanEngineSync(sync);
  renderOceanEngineAccountCenter(accountCenter);
  if (status.connected) stopOceanEngineStatusPolling(pollingIdentity.epoch, pollingIdentity.poller);
  return status;
}

function startOceanEngineStatusPolling() {
  if (oceanengineStatusPoller) clearInterval(oceanengineStatusPoller);
  const epoch = ++oceanengineStatusPollEpoch;
  let attempts = 0;
  const poller = setInterval(async () => {
    if (oceanengineStatusPollInFlight) return;
    oceanengineStatusPollInFlight = true;
    attempts += 1;
    try {
      const status = await refreshOceanEngineStatus();
      if (epoch !== oceanengineStatusPollEpoch || status.stale) return;
      if (status.connected || !status.authorization_in_progress || attempts >= 90) {
        stopOceanEngineStatusPolling(epoch, poller);
      }
    } catch {
      if (attempts >= 90) {
        stopOceanEngineStatusPolling(epoch, poller);
      }
    } finally {
      oceanengineStatusPollInFlight = false;
    }
  }, 2000);
  oceanengineStatusPoller = poller;
}

function scopedSelectedAccountKey(store = {}, selectedAccountKey = "") {
  const selected = String(selectedAccountKey || "").trim().toLowerCase();
  const linked = new Set((Array.isArray(store?.account_keys) ? store.account_keys : [])
    .map((value) => String(value || "").trim().toLowerCase())
    .filter(Boolean));
  return selected && linked.has(selected) ? selected : "";
}

function reconcileQianchuanScope(payload = {}, options = {}) {
  const available = options.available !== false;
  const accounts = available
    ? Array.isArray(payload?.stores)
      ? payload.stores
      : Array.isArray(payload?.accounts) ? payload.accounts : []
    : [];
  const requestedStoreKey = String(payload?.selected_store_key || "").trim().toLowerCase();
  const store = accounts.find((item) => String(item?.key || "").trim().toLowerCase() === requestedStoreKey) || null;
  const storeKey = store ? String(store.key || "").trim().toLowerCase() : "";
  const accountKey = storeKey ? scopedSelectedAccountKey(store, payload?.selected_account_key) : "";
  return { available, accounts, store, storeKey, accountKey };
}

function renderQianchuanAccounts(payload = {}, reconciliation = null) {
  const catalog = payload && typeof payload === "object" ? payload : {};
  currentQianchuanCatalog = catalog;
  const select = document.getElementById("qianchuan-account-select");
  const scope = reconciliation || reconcileQianchuanScope(catalog);
  const accounts = scope.accounts;
  const previousStoreKey = selectedStoreKey;
  const previousAccountKey = selectedQianchuanAccount;
  // The Agent catalog is the only source of truth. Reconciliation happens
  // before any capability or journey is derived, and a failed catalog read
  // closes both store and account scopes instead of borrowing browser state.
  const activeKey = scope.storeKey;
  const analysisAccount = scope.store;
  const linkedKeys = Array.isArray(analysisAccount?.account_keys) ? analysisAccount.account_keys : [];
  const scopedAccountKey = scope.accountKey;
  select.replaceChildren();
  const current = document.createElement("option");
  current.value = "";
  current.textContent = accounts.length ? "请选择" : "暂无可用数据";
  select.append(current);
  accounts.forEach((account) => {
    const option = document.createElement("option");
    option.value = account.key;
    const accountLabel = account.label || "抖店";
    const stateLabel = account.state_label || (account.channel === "official_api" ? "官方 API" : "网页");
    option.textContent = `${accountLabel} · ${stateLabel}`;
    select.append(option);
  });
  selectedStoreKey = activeKey;
  // The selected account is accepted only when the selected store explicitly
  // owns that account. A stale cross-endpoint selected_account_key is cleared.
  selectedQianchuanAccount = scopedAccountKey;
  const reconciledChengfangScope = chengfangPlanScope(activeKey, scopedAccountKey);
  if (previousStoreKey !== activeKey
    || previousAccountKey !== scopedAccountKey
    || currentChengfangLocalPlan.scope_key !== reconciledChengfangScope.scope_key) {
    switchChengfangLocalPlanScope(activeKey, scopedAccountKey);
  }
  restorePromotionBulkDraftsForCurrentScope();
  select.value = activeKey;
  if (scope.available) {
    chrome.storage.local.set({ scanStorePreference: activeKey, scanAccountPreference: selectedQianchuanAccount });
  }
  if (previousStoreKey !== activeKey) renderWorkbench();
  accountSelectionRequired = !activeKey;
  const scanButton = document.getElementById("full-scan-button");
  scanButton.disabled = fullScanRunning;
  scanButton.title = activeKey ? "按当前抖店开始巡检" : "打开抖店并自动准备后开始巡检";
  document.getElementById("active-store-name").textContent = analysisAccount?.label || "等待打开抖店";
  const workspaceContextLabel = document.getElementById("workspace-context-label");
  if (workspaceContextLabel) workspaceContextLabel.textContent = analysisAccount?.label || "等待打开抖店";
  document.getElementById("connection-center-store").textContent = analysisAccount?.label || "等待打开抖店";
  document.getElementById("connection-center-scope").textContent = analysisAccount
    ? selectedQianchuanAccount ? "抖店 + 当前千川" : "当前抖店"
    : "等待店铺";
  document.getElementById("store-mode-summary").textContent = analysisAccount
    ? `${catalog.store_count || accounts.length} 个店铺 · 当前${analysisAccount.state_label || "数据已隔离"} · 建议与日志仅使用本店数据`
    : "打开抖店首页后，系统会自动准备本次经营数据。";
  document.getElementById("qianchuan-account-hint").textContent = !scope.available
    ? "店铺与千川账户读取失败，投放范围已冻结；刷新连接状态后再继续。"
    : analysisAccount
    ? selectedQianchuanAccount
      ? `当前巡检固定为“${analysisAccount.label}”，并使用已确认关联的匿名千川账户。`
      : (analysisAccount.account_keys || []).length > 1
        ? `“${analysisAccount.label}”关联了多个千川账户；请选择本次巡检使用的一个账户，系统不会混合读取。`
        : `当前巡检固定为“${analysisAccount.label}”；先使用抖店数据，千川可稍后关联。`
    : "打开抖店后会自动准备经营数据；需要投放时再选择千川账户。";
  document.getElementById("connection-center-account").textContent = selectedQianchuanAccount
    ? `匿名账户 ${selectedQianchuanAccount.slice(-6).toUpperCase()}`
    : linkedKeys.length
      ? `${linkedKeys.length} 个待选择账户`
      : "按需连接";
  const linkedPanel = document.getElementById("store-linked-account");
  const linkedSelect = document.getElementById("linked-account-select");
  linkedPanel.hidden = !(activeKey && linkedKeys.length);
  linkedSelect.replaceChildren(...linkedKeys.map((accountKey) => {
    const option = document.createElement("option");
    option.value = accountKey;
    option.textContent = `匿名账户 ${String(accountKey).slice(-6).toUpperCase()}`;
    return option;
  }));
  linkedSelect.value = linkedKeys.includes(selectedQianchuanAccount) ? selectedQianchuanAccount : linkedKeys[0] || "";
  document.getElementById("store-linked-account-summary").textContent = linkedKeys.length > 1
    ? `当前店铺已关联 ${linkedKeys.length} 个千川账户；一次巡检只允许选择一个。`
    : "当前店铺已关联 1 个千川账户；可更换本次使用账户或安全解除关联。";
  document.getElementById("select-linked-account-button").disabled = !linkedSelect.value || linkedSelect.value === selectedQianchuanAccount;
  document.getElementById("unlink-account-button").disabled = !linkedSelect.value;
  document.getElementById("store-linked-account-result").textContent = selectedQianchuanAccount
    ? `本次已锁定匿名账户 ${selectedQianchuanAccount.slice(-6).toUpperCase()}`
    : linkedKeys.length ? "尚未选择本次巡检账户，千川页面不会加入巡检范围。" : "";
  const linkReview = document.getElementById("store-link-review");
  const unlinked = Array.isArray(catalog.unlinked_accounts) ? catalog.unlinked_accounts : [];
  linkReview.hidden = !(activeKey && unlinked.length);
  const unlinkedSelect = document.getElementById("unlinked-account-select");
  unlinkedSelect.replaceChildren(...unlinked.map((account) => {
    const option = document.createElement("option");
    option.value = account.key;
    option.textContent = qianchuanAccountEvidence(account).optionLabel;
    return option;
  }));
  renderSelectedQianchuanAccountEvidence(unlinked);
  if (currentChengfangCandidatePath) renderChengfangCandidatePath();
  else renderAutopilotCenter();
}

function promotionPlanMoney(value) {
  if (value === null || value === undefined || String(value).trim() === "") return "--";
  const number = Number(value);
  return Number.isFinite(number) ? `¥${number.toFixed(2)}` : "--";
}

function promotionPlanModeLabel(mode) {
  return {
    standard: "标准计划",
    full_domain: "全域推广",
    chengfang: "乘方推广",
    suixintui: "随心推",
    unknown: "模式待识别",
  }[mode] || "模式待识别";
}

function promotionPlanTypeLabel(type) {
  return { live: "直播计划", product: "商品计划", unknown: "类型待识别" }[type] || "类型待识别";
}

function promotionPlanAccountPayload(payload = {}) {
  const accounts = new Map();
  (Array.isArray(payload.accounts) ? payload.accounts : []).forEach((account) => {
    const key = String(account.account_key || account.key || "");
    if (key) accounts.set(key, account);
  });
  (currentOceanEngineAccountCenter?.accounts || []).forEach((account) => {
    const key = String(account.account_key || "");
    if (!key || accounts.has(key)) return;
    accounts.set(key, { account_key: key, account_label: account.display_name || account.platform_name || `账户 ${key.slice(-4)}` });
  });
  return {
    ...payload,
    selected_store_key: String(payload.selected_store_key || payload.store_key || selectedStoreKey || ""),
    selected_account_key: String(payload.selected_account_key || selectedQianchuanAccount || ""),
    accounts: [...accounts.values()],
  };
}

function syncPromotionPlanFilterControls() {
  const mappings = {
    "promotion-plan-account-filter": "account",
    "promotion-plan-mode-filter": "mode",
    "promotion-plan-type-filter": "plan_type",
    "promotion-plan-status-filter": "status",
    "promotion-plan-binding-filter": "binding",
  };
  Object.entries(mappings).forEach(([id, key]) => {
    const control = document.getElementById(id);
    if (control && [...control.options].some((option) => option.value === currentPromotionPlanFilters[key])) control.value = currentPromotionPlanFilters[key];
  });
  document.getElementById("promotion-plan-search").value = currentPromotionPlanFilters.query || "";
  document.querySelectorAll("#promotion-plan-feature-filters input").forEach((input) => {
    input.checked = (currentPromotionPlanFilters.features || []).includes(input.value);
  });
}

function renderPromotionPlanAccountOptions(view) {
  const accountFilter = document.getElementById("promotion-plan-account-filter");
  const currentFilterValue = currentPromotionPlanFilters.account || "all";
  const all = document.createElement("option");
  all.value = "all";
  all.textContent = "全部账户";
  accountFilter.replaceChildren(all, ...view.accounts.map((account) => {
    const option = document.createElement("option");
    option.value = account.key;
    option.textContent = account.label;
    return option;
  }));
  accountFilter.value = view.accounts.some((account) => account.key === currentFilterValue) ? currentFilterValue : "all";
  if (accountFilter.value !== currentFilterValue) currentPromotionPlanFilters.account = accountFilter.value;

  const batchAccount = document.getElementById("batch-plan-account");
  const currentBatchValue = batchAccount.value;
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = view.accounts.length ? "请选择已核对账户" : "尚无可用账户";
  batchAccount.replaceChildren(placeholder, ...view.accounts.map((account) => {
    const option = document.createElement("option");
    option.value = account.key;
    option.textContent = account.label;
    return option;
  }));
  batchAccount.value = view.accounts.some((account) => account.key === currentBatchValue) ? currentBatchValue : "";
}

function promotionPlanCell(primary, secondary = "") {
  const cell = document.createElement("td");
  const strong = document.createElement("strong");
  strong.textContent = primary;
  cell.append(strong);
  if (secondary) {
    const small = document.createElement("small");
    small.textContent = secondary;
    cell.append(small);
  }
  return cell;
}

function promotionPlanCapabilitiesCell(row) {
  const cell = document.createElement("td");
  const box = document.createElement("div");
  box.className = "promotion-plan-capabilities";
  if (row.stale) {
    const stale = document.createElement("span");
    stale.className = "danger";
    stale.textContent = `${row.freshness_label || "数据已过期"} · ${row.data_age_label || "请重新同步"}`;
    box.append(stale);
  }
  if (row.learning_phase && !["unavailable", "unknown"].includes(row.learning_phase)) {
    const learning = document.createElement("span");
    learning.className = row.learning_phase === "failed" ? "danger" : row.learning_phase === "learning" ? "warning" : "diagnostic";
    learning.textContent = row.learning_status_label;
    learning.title = `诊断来源：${row.diagnostic_source_label || "暂不可读"}`;
    box.append(learning);
  }
  if (row.platform_low_efficiency === "flagged") {
    const lowEfficiency = document.createElement("span");
    lowEfficiency.className = "danger";
    lowEfficiency.textContent = row.platform_low_efficiency_label || "平台标记低效";
    lowEfficiency.title = `诊断来源：${row.diagnostic_source_label || "官方 API"}`;
    box.append(lowEfficiency);
  }
  if (row.binding_paused) {
    const paused = document.createElement("span");
    paused.className = "warning";
    paused.textContent = "原绑定已暂停";
    box.append(paused);
  }
  if (row.binding) {
    const features = globalThis.DianPromotionPlanCenter.FEATURE_DEFINITIONS.filter((item) => row.binding.features.includes(item.id));
    features.slice(0, 4).forEach((feature) => {
      const chip = document.createElement("span");
      chip.textContent = feature.label;
      box.append(chip);
    });
    if (features.length > 4) {
      const more = document.createElement("span");
      more.textContent = `+${features.length - 4}`;
      box.append(more);
    }
  } else {
    const chip = document.createElement("span");
    chip.className = row.eligible_for_local_binding ? "" : "blocked";
    chip.textContent = row.eligible_for_local_binding ? "可建立本地托管" : row.binding_blockers[0] || "绑定条件待补齐";
    box.append(chip);
  }
  cell.append(box);
  return cell;
}

function promotionPlanActionCell(row) {
  const cell = document.createElement("td");
  const button = document.createElement("button");
  button.type = "button";
  const collectionReady = document.getElementById("promotion-plan-center")?.dataset.collectionReady === "true";
  const removableBinding = row.binding || row.saved_binding;
  button.dataset.promotionPlanAction = removableBinding ? "unbind" : "bind";
  button.dataset.planKey = row.plan_key;
  markAgentWriteControl(button);
  button.className = removableBinding ? `bound${row.binding_paused ? " paused" : ""}` : "";
  button.textContent = row.binding
    ? "解除本地绑定"
    : row.saved_binding
      ? "解除已暂停绑定"
      : row.eligible_for_local_binding ? "本地绑定" : "身份待补齐";
  button.disabled = !removableBinding && (!collectionReady || !row.eligible_for_local_binding);
  button.title = removableBinding
    ? `${removableBinding.profile_label || "本地策略"}；解除只删除本机档案，不影响千川计划`
    : !collectionReady
      ? "先完成当前账户、模式和计划类型的读取核验"
      : row.binding_blockers.join("；") || "建立可撤销的本地托管档案";
  cell.append(button);
  if (row.recommendation) {
    const small = document.createElement("small");
    small.textContent = row.recommendation;
    cell.append(small);
  }
  return cell;
}

function selectedPromotionPlans() {
  const rows = currentPromotionPlanView?.all_rows || [];
  return rows.filter((row) => selectedPromotionPlanKeys.has(row.plan_key));
}

function renderPromotionBulkSelection() {
  const plans = selectedPromotionPlans();
  const title = document.getElementById("promotion-bulk-selected-title");
  const list = document.getElementById("promotion-bulk-selected-list");
  const state = document.getElementById("promotion-bulk-action-state");
  const generate = document.getElementById("promotion-bulk-generate");
  title.textContent = plans.length ? `已选择 ${plans.length} 个计划` : "尚未选择计划";
  generate.disabled = !plans.length;
  if (!currentPromotionBulkActionDraft) {
    state.className = plans.length ? "warning" : "warning";
    state.textContent = plans.length ? "等待生成预演" : "尚未选择计划";
  }
  if (!plans.length) {
    list.className = "promotion-bulk-selected-list empty-state";
    list.textContent = "在投放总览勾选计划后，这里会按账户展示本次作用范围。";
    return;
  }
  list.className = "promotion-bulk-selected-list";
  list.replaceChildren(...plans.map((plan) => {
    const article = document.createElement("article");
    const copy = document.createElement("div");
    const strong = document.createElement("strong");
    const small = document.createElement("small");
    const account = document.createElement("span");
    const remove = document.createElement("button");
    strong.textContent = plan.plan_name;
    small.textContent = `${plan.plan_id || "计划 ID 缺失"} · 数据质量 ${plan.quality_score}`;
    copy.append(strong, small);
    account.textContent = `${plan.account_label} · ${promotionPlanModeLabel(plan.promotion_mode)}`;
    remove.type = "button";
    remove.dataset.promotionBulkRemove = plan.plan_key;
    remove.textContent = "移除";
    article.append(copy, account, remove);
    return article;
  }));
}

function renderPromotionPlanSelection() {
  const collectionReady = document.getElementById("promotion-plan-center")?.dataset.collectionReady === "true";
  const rowsByKey = new Map((currentPromotionPlanView?.all_rows || []).map((row) => [row.plan_key, row]));
  const available = new Set([...rowsByKey.values()]
    .filter((row) => row.supervised_draft_ready === true)
    .map((row) => row.plan_key));
  selectedPromotionPlanKeys = new Set([...selectedPromotionPlanKeys].filter((key) => available.has(key)));
  if (!collectionReady) selectedPromotionPlanKeys.clear();
  const visibleKeys = (currentPromotionPlanView?.rows || [])
    .slice(0, 200)
    .filter((row) => row.supervised_draft_ready === true)
    .map((row) => row.plan_key);
  const selectedVisible = visibleKeys.filter((key) => selectedPromotionPlanKeys.has(key)).length;
  const selectAll = document.getElementById("promotion-plan-select-all");
  selectAll.checked = Boolean(visibleKeys.length) && selectedVisible === visibleKeys.length;
  selectAll.indeterminate = selectedVisible > 0 && selectedVisible < visibleKeys.length;
  selectAll.disabled = !collectionReady || !visibleKeys.length;
  document.querySelectorAll("[data-promotion-plan-select]").forEach((input) => {
    const selected = selectedPromotionPlanKeys.has(input.value);
    const rowReady = rowsByKey.get(input.value)?.supervised_draft_ready === true;
    input.checked = selected;
    input.disabled = !collectionReady || !rowReady;
    input.title = !collectionReady
      ? "完成当前计划范围读取与新鲜度核验后才可选择"
      : rowReady ? "" : "当前计划仍有身份、新鲜度或质量阻塞";
    input.closest("tr")?.classList.toggle("is-selected", selected);
  });
  const count = selectedPromotionPlanKeys.size;
  document.getElementById("promotion-plan-selected-count").textContent = collectionReady ? `已选 ${count} 项` : "范围未核验，暂不可预演";
  document.getElementById("promotion-plan-open-bulk").disabled = !collectionReady || !count;
  document.getElementById("promotion-plan-clear-selection").disabled = !count;
  renderPromotionBulkSelection();
}

function resetPromotionBulkActionDraft() {
  currentPromotionBulkActionDraft = null;
  renderPromotionBulkActionDraft();
}

function currentPromotionBulkScope() {
  return { store_key: selectedStoreKey, account_key: selectedQianchuanAccount };
}

function restorePromotionBulkDraftsForCurrentScope() {
  if (!globalThis.DianPromotionBulkActions) return;
  currentPromotionBulkActionDrafts = globalThis.DianPromotionBulkActions.filterDraftsForScope(
    allPromotionBulkActionDrafts,
    currentPromotionBulkScope(),
  ).slice(0, 20);
  currentPromotionBulkActionDraft = currentPromotionBulkActionDrafts[0] || null;
  if (document.getElementById("promotion-bulk-preview-title")) renderPromotionBulkActionDraft();
}

function syncPromotionBulkActionControls() {
  const action = document.getElementById("promotion-bulk-action-type").value;
  const definition = globalThis.DianPromotionBulkActions?.ACTIONS?.[action];
  document.getElementById("promotion-bulk-budget-field").hidden = action !== "decrease_budget";
  document.getElementById("promotion-bulk-schedule-fields").hidden = action !== "schedule_plan";
  document.getElementById("promotion-bulk-action-description").textContent = definition?.description || "请选择批量动作。";
}

function promotionBulkValue(value) {
  if (value === null || value === undefined || String(value).trim() === "") return "--";
  if (typeof value === "number") return Number(value.toFixed(2)).toLocaleString("zh-CN");
  return String(value);
}

function renderPromotionBulkActionDraft(draft = currentPromotionBulkActionDraft) {
  const state = document.getElementById("promotion-bulk-action-state");
  const title = document.getElementById("promotion-bulk-preview-title");
  const summary = document.getElementById("promotion-bulk-preview-summary");
  const list = document.getElementById("promotion-bulk-preview-list");
  const copy = document.getElementById("promotion-bulk-copy");
  const notice = document.getElementById("promotion-bulk-notice");
  if (!draft?.items?.length) {
    title.textContent = "尚未生成批量草稿";
    summary.replaceChildren();
    list.className = "promotion-bulk-preview-list empty-state";
    list.textContent = "生成后会列出可复核计划、阻塞原因和变更前后值。";
    copy.disabled = true;
    notice.textContent = "只在本机生成草稿；不会自动点击页面，也不会提交千川。";
    return;
  }
  const blocked = Number(draft.summary?.blocked || 0);
  const ready = Number(draft.summary?.ready || 0);
  state.className = blocked === 0 ? "safe" : ready ? "warning" : "danger";
  state.textContent = blocked === 0 ? `${ready} 项可逐项复核` : ready ? `${ready} 项可复核 · ${blocked} 项阻塞` : `${blocked} 项全部阻塞`;
  title.textContent = `${draft.action_label} · ${draft.items.length} 个计划`;
  const summaryItems = [
    ["选中计划", `${draft.summary.total} 项`],
    ["可逐项复核", `${ready} 项`],
    ["暂时阻塞", `${blocked} 项`],
    [draft.summary.current_budget === null ? "账户范围" : "预算变化", draft.summary.current_budget === null ? `${draft.summary.accounts} 个账户` : `¥${promotionBulkValue(draft.summary.current_budget)} → ¥${promotionBulkValue(draft.summary.target_budget)}`],
  ];
  summary.replaceChildren(...summaryItems.map(([label, value]) => {
    const article = document.createElement("article");
    const span = document.createElement("span");
    const strong = document.createElement("strong");
    span.textContent = label;
    strong.textContent = value;
    article.append(span, strong);
    return article;
  }));
  list.className = "promotion-bulk-preview-list";
  list.replaceChildren(...draft.items.map((item) => {
    const article = document.createElement("article");
    article.className = item.state === "blocked" ? "blocked" : "";
    const identity = document.createElement("div");
    const strong = document.createElement("strong");
    const small = document.createElement("small");
    const change = document.createElement("span");
    const badge = document.createElement("b");
    strong.textContent = item.plan_name;
    small.textContent = item.state === "blocked" ? item.blockers.join("；") : `${item.account_label} · 质量 ${item.quality_score}`;
    identity.append(strong, small);
    change.textContent = `${item.field}：${promotionBulkValue(item.current_value)} → ${promotionBulkValue(item.target_value)}`;
    badge.textContent = item.state === "blocked" ? "暂不放行" : "待逐项复核";
    article.append(identity, change, badge);
    return article;
  }));
  copy.disabled = false;
  notice.textContent = draft.notice;
}

async function generatePromotionBulkActionDraft() {
  const input = {
    action: document.getElementById("promotion-bulk-action-type").value,
    decrease_percent: document.getElementById("promotion-bulk-decrease-percent").value,
    schedule_start: document.getElementById("promotion-bulk-schedule-start").value,
    schedule_end: document.getElementById("promotion-bulk-schedule-end").value,
    plan_keys: [...selectedPromotionPlanKeys],
    store_key: selectedStoreKey,
    account_key: selectedQianchuanAccount,
  };
  const draft = globalThis.DianPromotionBulkActions.buildBulkDraft(currentPromotionPlanView?.all_rows || [], input);
  allPromotionBulkActionDrafts = [draft, ...allPromotionBulkActionDrafts.filter((item) => item?.draft_id !== draft.draft_id)].slice(0, 20);
  await chrome.storage.local.set({ [PROMOTION_BULK_ACTION_DRAFTS_KEY]: allPromotionBulkActionDrafts });
  restorePromotionBulkDraftsForCurrentScope();
  const scopedDraft = currentPromotionBulkActionDraft || draft;
  renderPromotionBulkActionDraft(scopedDraft);
  await recordAutopilotActivity("bulk_preview", scopedDraft.action_label, `${scopedDraft.summary.ready} 项可复核，${scopedDraft.summary.blocked} 项阻塞；未提交千川。`);
  return scopedDraft;
}

function renderPromotionOperationLog(payload = currentPromotionOperationAudit) {
  if (!globalThis.DianPromotionOperationLog) return;
  currentPromotionOperationAudit = payload && typeof payload === "object" ? payload : { actions: [], execution_enabled: false };
  const view = globalThis.DianPromotionOperationLog.deriveView(currentPromotionOperationAudit, currentPromotionOperationLogFilters);
  currentPromotionOperationLogView = view;
  const state = document.getElementById("promotion-operation-log-state");
  state.className = view.safe ? "safe" : "danger";
  state.textContent = view.safe ? "真实写入关闭" : "检测到异常执行开关";
  document.getElementById("promotion-operation-log-time").textContent = view.updated_at || "本机日志已读取";
  document.getElementById("promotion-log-total").textContent = String(view.summary.total);
  document.getElementById("promotion-log-visible").textContent = String(view.summary.visible);
  document.getElementById("promotion-log-confirmed").textContent = String(view.summary.confirmed);
  document.getElementById("promotion-log-executed").textContent = String(view.summary.executed);
  document.getElementById("promotion-log-cancelled").textContent = String(view.summary.cancelled);
  document.getElementById("promotion-operation-log-notice").textContent = view.notice;
  document.getElementById("promotion-log-export").disabled = !view.rows.length;
  const body = document.getElementById("promotion-operation-log-body");
  const table = body.closest("table");
  const empty = document.getElementById("promotion-operation-log-empty");
  table.hidden = !view.rows.length;
  empty.hidden = Boolean(view.rows.length);
  if (!view.rows.length) {
    empty.textContent = view.all_rows.length ? "当前筛选条件下没有日志，请调整筛选条件。" : "暂无操作记录。动作必须进入本机审计后才会出现在这里。";
    body.replaceChildren();
    return;
  }
  body.replaceChildren(...view.rows.map((row) => {
    const tr = document.createElement("tr");
    const time = promotionPlanCell(row.timestamp ? new Date(row.timestamp).toLocaleString("zh-CN", { hour12: false }) : "时间待确认", row.action_id ? `动作 ${row.action_id}` : "动作编号待生成");
    const action = promotionPlanCell(row.operation_label, row.operation_type);
    const target = promotionPlanCell(row.target_name, `${row.account_label}${row.target_id ? ` · ${row.target_id}` : ""}`);
    const change = promotionPlanCell(row.change_label);
    const stateCell = document.createElement("td");
    const stateBadge = document.createElement("span");
    stateBadge.className = `log-state ${row.state_tone}`;
    stateBadge.textContent = row.state_label;
    stateCell.append(stateBadge);
    const evidence = promotionPlanCell(row.evidence_label);
    tr.append(time, action, target, change, stateCell, evidence);
    return tr;
  }));
}

function readPromotionOperationLogFilters() {
  currentPromotionOperationLogFilters = globalThis.DianPromotionOperationLog.normalizeFilters({
    operation_type: document.getElementById("promotion-log-operation-filter").value,
    state: document.getElementById("promotion-log-state-filter").value,
    query: document.getElementById("promotion-log-search").value,
    date_from: document.getElementById("promotion-log-date-from").value,
    date_to: document.getElementById("promotion-log-date-to").value,
  });
  renderPromotionOperationLog();
}

async function refreshPromotionOperationLog() {
  const payload = await bridgeFetch("/actions/audit?limit=500");
  renderPromotionOperationLog(payload);
  return payload;
}

function derivePromotionPlanCollectionRecovery(receipt = {}, selection = {}) {
  const warningCodes = new Set((Array.isArray(receipt?.coverage_warnings) ? receipt.coverage_warnings : [])
    .map((warning) => String(warning?.code || warning || "").trim().toUpperCase())
    .filter(Boolean));
  const has = (...codes) => codes.some((code) => warningCodes.has(code));
  const scope = selection?.scope && typeof selection.scope === "object" ? selection.scope : {};
  const account = String(scope.account || "all").trim().toLowerCase();
  const mode = String(scope.promotion_mode || "unknown").trim().toLowerCase();
  const planType = String(scope.plan_type || "all").trim().toLowerCase();
  const planTypeLabel = planType === "live" ? "直播计划" : planType === "product" ? "商品计划" : "投放计划";
  const collectedRows = Math.max(0, Number(receipt?.collected_rows) || 0);
  const platformTotalRaw = receipt?.platform_total;
  const platformTotal = platformTotalRaw === null || platformTotalRaw === undefined || String(platformTotalRaw).trim() === ""
    ? null
    : Number.isFinite(Number(platformTotalRaw)) ? Math.max(0, Number(platformTotalRaw)) : null;
  const unlinkedAccount = selection?.unlinked_account && typeof selection.unlinked_account === "object"
    ? selection.unlinked_account
    : null;
  if (unlinkedAccount) {
    const accountKey = String(unlinkedAccount.account_key || unlinkedAccount.key || "").trim();
    const accountLabel = String(unlinkedAccount.label || unlinkedAccount.account_label || "").trim();
    const accountHint = accountLabel || (accountKey ? `尾号 ${accountKey.slice(-6).toUpperCase()}` : "新账户");
    return {
      warning_code: "UNLINKED_QIANCHUAN_ACCOUNT",
      kind: "review_unlinked_account",
      state: "blocked",
      blocked: true,
      headline: "发现未关联的千川账户",
      blocker: `最近页面来自尚未关联的${accountHint}；继续读取可能把计划归到错误店铺。`,
      label: "关联或切换千川账户",
      instruction: "先在店铺连接中核对账户归属；系统不会自动关联，也不会继续投放操作。",
    };
  }
  if (selection?.source === "scope_missing") {
    return {
      warning_code: "SCOPED_RECEIPT_MISSING",
      kind: "sync_page",
      state: "missing",
      blocked: true,
      headline: `当前${planTypeLabel}范围尚未读取`,
      blocker: `这个账户与模式下的${planTypeLabel}还没有独立读取凭证。`,
      label: `读取当前${planTypeLabel}`,
      instruction: `打开对应千川${planTypeLabel}页后只读同步；不会修改任何计划。`,
    };
  }
  if (has("PLATFORM_TOTAL_CONFLICT")) {
    return {
      warning_code: "PLATFORM_TOTAL_CONFLICT",
      kind: "review_account",
      state: "blocked",
      blocked: true,
      headline: "账户范围存在冲突",
      blocker: "同一批数据出现了不同的平台计划总数，当前账户可能不一致。",
      label: "重新核对正确账户",
      instruction: "页面总数口径冲突。先核对当前千川账户，再重新读取计划。",
    };
  }
  if (has("COLLECTED_ROWS_EXCEED_PLATFORM_TOTAL", "MULTIPLE_TOTAL_SCOPES")) {
    return {
      warning_code: has("COLLECTED_ROWS_EXCEED_PLATFORM_TOTAL") ? "COLLECTED_ROWS_EXCEED_PLATFORM_TOTAL" : "MULTIPLE_TOTAL_SCOPES",
      kind: "narrow_scope",
      state: "blocked",
      blocked: true,
      headline: "多个计划范围混在一起",
      blocker: "本次数据混入了多个账户、模式或计划类型，不能据此判断完整性。",
      label: "选择单一账户与模式",
      instruction: "当前混入多个读取范围。一次只选择一个账户、一种模式和一种计划类型。",
    };
  }
  const hasSpecificRecovery = has(
    "PAGINATION_TRUNCATED",
    "COLLECTED_ROWS_BELOW_PLATFORM_TOTAL",
    "LOCAL_LIST_TRUNCATED",
    "STABLE_PLAN_ID_INCOMPLETE",
    "PROMOTION_MODE_INCOMPLETE",
  );
  if (selection?.exact_scope !== true && !hasSpecificRecovery) {
    if (collectedRows === 0 && platformTotal !== 0) {
      return {
        warning_code: "PLAN_DATA_MISSING",
        kind: "sync_page",
        state: "missing",
        blocked: true,
        headline: "尚未读取到计划",
        blocker: `本机还没有可用于判断的${planTypeLabel}数据。`,
        label: `读取当前${planTypeLabel}`,
        instruction: `打开千川${planTypeLabel}列表后只读同步；不会修改任何计划。`,
      };
    }
    if (account === "all") {
      return {
        warning_code: "ACCOUNT_SCOPE_REQUIRED",
        kind: "focus_account",
        state: "scope",
        blocked: true,
        headline: "先锁定一个广告账户",
        blocker: "全部账户视图只能浏览，不能证明某个账户的计划读取完整。",
        label: "选择一个广告账户",
        instruction: "先锁定广告账户，再逐步确认投放模式和计划类型。",
      };
    }
    if (mode === "unknown") {
      return {
        warning_code: "MODE_SCOPE_REQUIRED",
        kind: "focus_mode",
        state: "scope",
        blocked: true,
        headline: "再锁定一种投放模式",
        blocker: "当前模式范围过宽，直播、商品或乘方数据可能被混在一起。",
        label: "选择一种投放模式",
        instruction: "一次只核验一种投放模式，避免把不同口径误判为完整。",
      };
    }
    return {
      warning_code: "PLAN_TYPE_SCOPE_REQUIRED",
      kind: "focus_plan_type",
      state: "scope",
      blocked: true,
      headline: "区分直播计划与商品计划",
      blocker: "直播与商品计划来自不同列表，必须分别确认读取范围。",
      label: "选择直播或商品计划",
      instruction: "选择一种计划类型后，只读取对应列表。",
    };
  }
  if (receipt?.safe_to_claim_complete === true) {
    if (String(receipt?.status || "").trim().toLowerCase() === "confirmed_empty" || platformTotal === 0) {
      return {
        warning_code: "COVERAGE_CONFIRMED_EMPTY",
        kind: "switch_plan_type",
        state: "empty",
        blocked: false,
        headline: `平台已确认当前没有${planTypeLabel}`,
        blocker: "当前范围已核验完成，没有可进入诊断的计划。",
        label: "查看另一类计划",
        instruction: "切换直播或商品计划类型；该操作不会创建或修改计划。",
      };
    }
    if (collectedRows === 0) {
      return {
        warning_code: "COMPLETE_RECEIPT_WITHOUT_ROWS",
        kind: "sync_page",
        state: "blocked",
        blocked: true,
        headline: `${planTypeLabel}凭证与计划行不一致`,
        blocker: "凭证标记完整，但没有读取到计划行；为避免误判，当前保持只读阻塞。",
        label: `重新读取${planTypeLabel}`,
        instruction: "重新读取当前列表并核对平台总数；不会修改任何计划。",
      };
    }
    const scopeRows = Array.isArray(selection?.scope_rows) ? selection.scope_rows : [];
    const diagnosisReadyRows = scopeRows.filter((row) => row?.read_only_diagnosis_ready === true);
    const nextAction = globalThis.DianPromotionPlanCenter.selectScopeRecoveryAction(scopeRows);
    if (nextAction?.code === "reselect_verified_scope") {
      return {
        warning_code: "PLAN_SCOPE_IDENTITY_MISMATCH",
        kind: "reselect_verified_scope",
        state: "blocked",
        blocked: true,
        headline: `${planTypeLabel}身份与读取范围不一致`,
        blocker: nextAction.detail || "采集回执与当前账户、投放模式或计划类型不一致。",
        label: nextAction.label || "重新核对计划身份",
        instruction: "先核对店铺与千川账户归属，再回到单一账户、模式和计划类型范围重新读取。",
      };
    }
    if (nextAction?.code === "read_plan_id") {
      return {
        warning_code: "PLAN_IDENTITY_UNVERIFIED",
        kind: "sync_stable_ids",
        state: "partial",
        blocked: true,
        headline: `${planTypeLabel}缺少稳定计划 ID`,
        blocker: nextAction.detail || "没有稳定计划 ID，不能把诊断或草稿绑定到该计划。",
        label: planType === "live" ? "打开直播计划并补读 ID" : "打开商品计划并补读 ID",
        instruction: `打开正确账户的千川${planTypeLabel}列表，只读采集稳定 ID 后重新验证；不会猜测或生成计划 ID。`,
      };
    }
    if (nextAction?.code === "continue_scoped_collection") {
      return {
        warning_code: "PLAN_SCOPE_COVERAGE_INCOMPLETE",
        kind: "continue_pagination",
        state: "partial",
        blocked: true,
        headline: `${planTypeLabel}范围尚未读完`,
        blocker: nextAction.detail || "当前范围缺少完整翻页或滚动采集凭证。",
        label: `打开并继续读取${planTypeLabel}`,
        instruction: `保持当前账户、模式和${planTypeLabel}不变，继续只读采集并重新验证。`,
      };
    }
    const staleRows = scopeRows.filter((row) => row?.collection_gate_state === "complete_but_stale" || row?.stale === true);
    if (scopeRows.length > 0 && diagnosisReadyRows.length === 0 && staleRows.length === scopeRows.length) {
      return {
        warning_code: "SCOPED_ROWS_STALE",
        kind: "refresh_verified_scope",
        state: "partial",
        blocked: true,
        headline: `${planTypeLabel}范围完整，但数据已过期`,
        blocker: `${scopeRows.length} 条计划都已超过诊断新鲜度，旧数据不能生成当前建议。`,
        label: `打开并刷新当前${planTypeLabel}`,
        instruction: `保持当前账户、模式和${planTypeLabel}不变，打开对应计划列表重新只读采集并验证。`,
      };
    }
    if (diagnosisReadyRows.length === 0) {
      return {
        warning_code: "SCOPED_ROWS_NOT_DIAGNOSIS_READY",
        kind: "refresh_verified_scope",
        state: "blocked",
        blocked: true,
        headline: `${planTypeLabel}尚不能进入诊断`,
        blocker: String(nextAction?.detail || "读取范围完整，但计划身份、新鲜度或数据质量仍未通过。"),
        label: String(nextAction?.label || `补齐${planTypeLabel}证据`),
        instruction: "按当前计划行给出的修复动作补齐证据；真实投放写入仍关闭。",
      };
    }
    return {
      warning_code: "COVERAGE_CONFIRMED",
      kind: "review_plans",
      state: "ready",
      blocked: false,
      headline: `当前${planTypeLabel}读取完整`,
      blocker: "账户、模式、类型和覆盖范围均已确认，可以进入只读诊断。",
      label: `查看${planTypeLabel}诊断`,
      instruction: "只查看风险与建议；不会开启或执行千川生产写入。",
    };
  }
  if (has("PAGINATION_TRUNCATED", "COLLECTED_ROWS_BELOW_PLATFORM_TOTAL")) {
    return {
      warning_code: has("PAGINATION_TRUNCATED") ? "PAGINATION_TRUNCATED" : "COLLECTED_ROWS_BELOW_PLATFORM_TOTAL",
      kind: "continue_pagination",
      state: "partial",
      blocked: true,
      headline: `${planTypeLabel}只读取了一部分`,
      blocker: platformTotal === null
        ? `已经读取 ${collectedRows} 条，但平台总数尚未确认。`
        : `平台显示 ${platformTotal} 条，目前只读取 ${collectedRows} 条。`,
      label: "继续翻页并读取",
      instruction: "计划还没读完。继续从当前千川计划页翻页或滚动，系统只读取不修改。",
    };
  }
  if (has("LOCAL_LIST_TRUNCATED")) {
    return {
      warning_code: "LOCAL_LIST_TRUNCATED",
      kind: "narrow_scope",
      state: "partial",
      blocked: true,
      headline: "本地展示达到上限",
      blocker: "当前范围太大，本地列表无法证明全部计划都已读取。",
      label: "缩小模式范围",
      instruction: "本地展示已到上限。选择单一账户、模式和计划类型后再读取。",
    };
  }
  if (has("STABLE_PLAN_ID_INCOMPLETE")) {
    return {
      warning_code: "STABLE_PLAN_ID_INCOMPLETE",
      kind: "sync_stable_ids",
      state: "partial",
      blocked: true,
      headline: "部分计划缺少稳定 ID",
      blocker: "没有稳定计划 ID，后续诊断可能关联到错误计划。",
      label: "补齐稳定计划 ID",
      instruction: "部分计划没有稳定 ID。请在千川显示完整计划列表后重新读取；补齐前保持只读。",
    };
  }
  if (has("PROMOTION_MODE_INCOMPLETE")) {
    return {
      warning_code: "PROMOTION_MODE_INCOMPLETE",
      kind: "narrow_mode",
      state: "partial",
      blocked: true,
      headline: "部分计划模式未确认",
      blocker: "计划模式不明确，不能确定应使用哪套指标和策略。",
      label: "按单一模式重新读取",
      instruction: "部分计划模式未确认。先选择一种投放模式，再读取对应计划页。",
    };
  }
  return {
    warning_code: has("PLATFORM_TOTAL_UNOBSERVED") ? "PLATFORM_TOTAL_UNOBSERVED" : "COVERAGE_UNVERIFIED",
    kind: "sync_page",
    state: collectedRows ? "partial" : "missing",
    blocked: true,
    headline: collectedRows ? `${planTypeLabel}覆盖范围待确认` : `尚未读取到${planTypeLabel}`,
    blocker: collectedRows
      ? `已读取 ${collectedRows} 条，但平台总数或完整范围还没有证据。`
      : `本机还没有可确认的${planTypeLabel}数据。`,
    label: `同步当前${planTypeLabel}`,
    instruction: "先打开正确账户的千川计划列表并完成加载，再进行只读同步。",
  };
}

function renderPromotionPlanCollectionReceipt(receipt = {}, selection = {}) {
  const container = document.getElementById("promotion-plan-collection-receipt");
  const center = document.getElementById("promotion-plan-center");
  const stateLabel = document.getElementById("promotion-plan-coverage-state-label");
  const title = document.getElementById("promotion-plan-coverage-title");
  const detail = document.getElementById("promotion-plan-coverage-detail");
  const blockerLabel = document.getElementById("promotion-plan-coverage-blocker-label");
  const warning = document.getElementById("promotion-plan-coverage-warning");
  const action = document.getElementById("promotion-plan-coverage-action");
  if (!container || !center || !stateLabel || !title || !detail || !blockerLabel || !warning || !action) return null;
  const scopedReceiptMissing = selection?.source === "scope_missing";
  const displayReceipt = scopedReceiptMissing
    ? { ...receipt, safe_to_claim_complete: false, coverage_complete: false }
    : receipt;
  const view = globalThis.DianPromotionPlanCenter.deriveCollectionReceiptView(displayReceipt);
  const recovery = derivePromotionPlanCollectionRecovery(displayReceipt, selection);
  const recoveryContract = globalThis.DianPromotionPlanCenter.buildScopeRecoveryContract(selection);
  currentPromotionPlanRecovery = { recovery, selection, contract: recoveryContract };
  const visualTone = recovery.state === "ready" || recovery.state === "empty"
    ? "complete"
    : recovery.state === "partial"
      ? "attention"
      : recovery.state === "blocked"
        ? "blocked"
        : view.tone === "complete" ? "attention" : view.tone;
  container.className = `promotion-plan-collection-receipt ${visualTone}`;
  container.dataset.recoveryCode = recovery.warning_code;
  container.dataset.receiptSource = String(selection?.source || "global");
  container.dataset.journeyState = recovery.state;
  center.dataset.collectionReady = String(recovery.state === "ready");
  stateLabel.textContent = recovery.state === "ready"
    ? "读取完成 · 只读"
    : recovery.state === "empty"
      ? "范围已核验"
      : "数据准备未完成";
  blockerLabel.textContent = recovery.blocked ? "阻塞原因" : recovery.state === "ready" ? "当前状态" : "范围结论";
  title.textContent = recovery.headline || (scopedReceiptMissing
    ? "当前筛选尚无读取凭证"
    : selection?.source === "scoped"
      ? `当前筛选 · ${view.title}`
      : view.title);
  detail.textContent = scopedReceiptMissing
    ? "当前筛选范围尚未单独读取 · 全局数据不能证明该范围完整"
    : view.detail;
  warning.textContent = recovery.blocker || recovery.instruction;
  action.dataset.recoveryKind = recovery.kind;
  action.dataset.recoveryCode = recovery.warning_code;
  action.dataset.recoveryPurpose = recoveryContract.purpose;
  action.dataset.recoveryPageIds = recoveryContract.page_ids.join(",");
  action.dataset.recoveryExpectedPageTypes = recoveryContract.expected_page_types.join(",");
  action.textContent = recovery.label;
  action.setAttribute("aria-label", `${recovery.label}。${recovery.instruction}`);
  return recovery;
}

function promotionPlanRecoveryError(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

async function waitForPromotionPlanRecoveryScan(runId, contract, timeoutMs = SCAN_TIMEOUT_MS) {
  const terminalStates = new Set(["completed", "partial", "cancelled", "interrupted", "error"]);
  const deadline = Date.now() + Math.max(5000, Number(timeoutMs) || SCAN_TIMEOUT_MS);
  while (Date.now() < deadline) {
    const response = await chrome.runtime.sendMessage({ type: "get-dashboard" });
    if (!response?.ok) throw promotionPlanRecoveryError("RECOVERY_STATUS_UNAVAILABLE", response?.error || "无法读取计划采集进度");
    const scan = response.dashboard?.fullScan || {};
    if (String(scan.run_id || "") === String(runId || "") && terminalStates.has(String(scan.status || ""))) {
      if (["cancelled", "interrupted", "error"].includes(scan.status)) {
        throw promotionPlanRecoveryError(scan.error_code || "RECOVERY_SCAN_FAILED", scan.error || "当前计划范围读取未完成");
      }
      const expectedIds = new Set(contract.page_ids);
      const result = (Array.isArray(scan.results) ? scan.results : []).find((item) => expectedIds.has(String(item?.id || "")));
      if (!result || result.ok !== true) {
        throw promotionPlanRecoveryError(result?.error_code || scan.error_code || "RECOVERY_PAGE_FAILED", result?.error || "正确计划页没有返回可验证数据");
      }
      return scan;
    }
    await new Promise((resolve) => setTimeout(resolve, 700));
  }
  throw promotionPlanRecoveryError("RECOVERY_SCAN_TIMEOUT", "计划范围读取超时；本次没有开启或执行生产写入");
}

async function runPromotionPlanScopeRecovery(contractValue = {}) {
  const selection = currentPromotionPlanRecovery?.selection || {};
  const contract = globalThis.DianPromotionPlanCenter.buildScopeRecoveryContract(selection);
  if (contractValue?.purpose && contractValue.purpose !== contract.purpose) {
    throw promotionPlanRecoveryError("RECOVERY_SCOPE_CHANGED", "当前计划范围已经变化，请按最新提示重新操作");
  }
  if (!contract.can_collect || contract.platform_write_enabled !== false || contract.automatic_submit !== false) {
    throw promotionPlanRecoveryError("RECOVERY_SCOPE_REQUIRED", "请先选择单一广告账户、投放模式和计划类型");
  }
  if (!selectedStoreKey) {
    await ensureCurrentStoreForScan();
  }
  if (!selectedStoreKey) {
    throw promotionPlanRecoveryError("STORE_SELECTION_UNCONFIRMED", "请先打开抖店首页，再读取对应千川计划。");
  }
  const response = await chrome.runtime.sendMessage({
    type: "start-full-scan",
    scan_scope: "quick",
    store_key: selectedStoreKey,
    account_key: contract.account_key,
    page_ids: [...contract.page_ids],
    recovery_purpose: contract.purpose,
    expected_page_types: [...contract.expected_page_types],
  });
  if (!response?.ok || response.started !== true || !response.run_id) {
    throw promotionPlanRecoveryError(
      response?.code || response?.error_code || "RECOVERY_SCAN_NOT_STARTED",
      response?.error || response?.message || "当前有其他巡检正在进行，请完成后再读取计划范围",
    );
  }
  const scan = await waitForPromotionPlanRecoveryScan(response.run_id, contract);
  const payload = await bridgeFetch(contract.verification_endpoint);
  renderPromotionPlanConsole(payload);
  return { contract, scan, payload, recovery: currentPromotionPlanRecovery?.recovery || null };
}

async function runPromotionPlanCollectionRecovery(button) {
  const kind = String(button?.dataset?.recoveryKind || "sync_page");
  const notice = document.getElementById("promotion-plan-notice");
  if (kind === "review_unlinked_account") {
    navigateToWorkspaceElement("promotion-plan-center", { role: "直播投放", workspaceKey: "promotion-overview" });
    document.getElementById("promotion-plan-account-filter")?.focus();
    if (notice) notice.textContent = "检测到新的千川账户。请打开要查看的账户页面并同步；系统不会猜测或继续投放。";
    return;
  }
  if (kind === "review_account" || kind === "reselect_verified_scope") {
    navigateToWorkspaceElement("promotion-plan-center", { role: "直播投放", workspaceKey: "promotion-overview" });
    const account = document.getElementById("promotion-plan-account-filter");
    account?.focus();
    if (notice) notice.textContent = "选择本次要查看的千川账户，再同步对应计划页。";
    return;
  }
  if (kind === "narrow_scope" || kind === "narrow_mode") {
    navigateToWorkspaceElement("promotion-plan-center", { role: "直播投放", workspaceKey: "promotion-overview" });
    const target = kind === "narrow_mode"
      ? document.getElementById("promotion-plan-mode-filter")
      : document.getElementById(currentPromotionPlanFilters.account === "all" ? "promotion-plan-account-filter" : "promotion-plan-mode-filter");
    target?.focus();
    if (notice) notice.textContent = kind === "narrow_mode"
      ? "选择一种投放模式后，再点击“同步并刷新”。"
      : "依次选择单一账户、投放模式和计划类型后，再同步对应计划页。";
    return;
  }
  if (["focus_account", "focus_mode", "focus_plan_type", "switch_plan_type"].includes(kind)) {
    navigateToWorkspaceElement("promotion-plan-center", { role: "直播投放", workspaceKey: "promotion-overview" });
    const targetId = kind === "focus_account"
      ? "promotion-plan-account-filter"
      : kind === "focus_mode"
        ? "promotion-plan-mode-filter"
        : "promotion-plan-type-filter";
    document.getElementById(targetId)?.focus();
    if (notice) notice.textContent = kind === "switch_plan_type"
      ? "当前范围已确认没有计划；可切换直播或商品计划继续查看。"
      : "按提示只完成这一项范围选择，页面会立即给出下一步。";
    return;
  }
  if (kind === "review_plans") {
    navigateToWorkspaceElement("promotion-plan-center", { role: "直播投放", workspaceKey: "promotion-overview" });
    const table = document.querySelector("#promotion-plan-center .promotion-plan-table-wrap");
    table?.scrollIntoView({ behavior: "smooth", block: "start" });
    document.querySelector("#promotion-plan-table-body [data-promotion-plan-select]")?.focus();
    if (notice) notice.textContent = "已进入只读计划诊断；真实投放写入仍关闭。";
    return;
  }
  const idleLabel = button.textContent;
  button.disabled = true;
  button.textContent = ["continue_pagination", "sync_stable_ids", "refresh_verified_scope", "sync_page"].includes(kind)
    ? "正在打开正确页面并读取…"
    : "正在只读同步…";
  try {
    const result = await runPromotionPlanScopeRecovery(currentPromotionPlanRecovery?.contract || {});
    const next = result.recovery;
    if (notice) notice.textContent = next?.state === "ready"
      ? "当前账户、模式、类型和数据新鲜度已重新验证，可以进入只读诊断；生产写入仍关闭。"
      : `已完成当前范围只读采集；仍需：${next?.label || "按最新阻塞原因继续补齐"}。`;
  } catch (error) {
    if (["ACCOUNT_SCOPE_MISMATCH", "ACCOUNT_MISMATCH", "ACCOUNT_IDENTITY_CONFLICT"].includes(error?.code)) {
      const forcedSelection = globalThis.DianPromotionPlanCenter.selectCollectionReceipt(
        currentPromotionPlanConsole,
        currentPromotionPlanFilters,
      );
      forcedSelection.unlinked_account = currentQianchuanCatalog?.unlinked_accounts?.[0]
        || { label: "当前千川页面账户" };
      renderPromotionPlanCollectionReceipt(forcedSelection.receipt, forcedSelection);
      renderPromotionPlanSelection();
      if (notice) notice.textContent = "当前页面账户与已选账户不一致；本次没有保存，请先关联或切换账户。";
    } else if (error?.code === "STORE_SELECTION_UNCONFIRMED") {
      if (notice) notice.textContent = "请先打开抖店首页；系统会自动准备后再读取千川计划。";
    } else if (notice) {
      notice.textContent = `只读同步失败：${error.message || "请确认千川计划页已打开并完成登录"}`;
    }
  } finally {
    button.disabled = false;
    if (button.dataset.recoveryKind === kind) button.textContent = idleLabel;
  }
}

function renderPromotionPlanConsole(payload = currentPromotionPlanConsole) {
  if (!globalThis.DianPromotionPlanCenter) return;
  currentPromotionPlanConsole = promotionPlanAccountPayload(payload && typeof payload === "object" ? payload : {});
  let view = globalThis.DianPromotionPlanCenter.deriveView(currentPromotionPlanConsole, currentPromotionPlanFilters, currentPromotionPlanBindings);
  renderPromotionPlanAccountOptions(view);
  view = globalThis.DianPromotionPlanCenter.deriveView(currentPromotionPlanConsole, currentPromotionPlanFilters, currentPromotionPlanBindings);
  currentPromotionPlanView = view;
  syncPromotionPlanFilterControls();
  document.getElementById("promotion-plan-center").dataset.empty = String(!view.all_rows.length);

  const state = document.getElementById("promotion-plan-center-state");
  state.className = !view.safe || view.freshness_status === "future"
    ? "danger"
    : view.freshness_status === "stale" || view.freshness_status === "missing"
      ? "warning"
      : "safe";
  state.textContent = !view.all_rows.length
    ? "等待计划数据"
    : view.freshness_status === "future"
      ? "采集时间异常"
      : view.freshness_status === "stale"
        ? `数据已过期 · ${view.summary.stale} 个计划`
        : view.freshness_status === "missing"
          ? "采集时间待确认"
          : "只读计划已载入";
  document.getElementById("promotion-plan-center-time").textContent = view.all_rows.length
    ? `计划数据：${view.data_age_label}`
    : "尚未同步";
  document.getElementById("promotion-plan-visible").textContent = String(view.summary.visible);
  document.getElementById("promotion-plan-total").textContent = `共 ${view.summary.total} 个计划`;
  document.getElementById("promotion-plan-spend").textContent = promotionPlanMoney(view.summary.spend);
  document.getElementById("promotion-plan-roi").textContent = view.summary.weighted_roi === null ? "--" : Number(view.summary.weighted_roi).toFixed(2);
  document.getElementById("promotion-plan-bound").textContent = String(view.summary.bound);
  document.getElementById("promotion-plan-blocked").textContent = `${view.summary.blocked} 个待补齐`;
  document.getElementById("promotion-plan-notice").textContent = view.notice;
  const receiptSelection = globalThis.DianPromotionPlanCenter.selectCollectionReceipt(currentPromotionPlanConsole, currentPromotionPlanFilters);
  receiptSelection.scope_rows = view.all_rows.filter((row) => (
    currentPromotionPlanFilters.account !== "all"
      && row.account_key === currentPromotionPlanFilters.account
      && currentPromotionPlanFilters.mode !== "all"
      && row.promotion_mode === currentPromotionPlanFilters.mode
      && ["live", "product"].includes(currentPromotionPlanFilters.plan_type)
      && row.plan_type === currentPromotionPlanFilters.plan_type
  ));
  const accountGuard = globalThis.DianPromotionPlanCenter.deriveUnlinkedAccountGuard(
    currentPromotionPlanConsole,
    selectedStoreKey ? currentQianchuanCatalog || {} : {},
    currentPromotionPlanFilters,
  );
  receiptSelection.unlinked_account = accountGuard.blocked ? accountGuard.account : null;
  renderPromotionPlanCollectionReceipt(receiptSelection.receipt, receiptSelection);

  const body = document.getElementById("promotion-plan-table-body");
  const table = body.closest("table");
  const emptyState = document.getElementById("promotion-plan-empty");
  table.hidden = !view.rows.length;
  emptyState.hidden = Boolean(view.rows.length);
  if (!view.rows.length) {
    emptyState.textContent = view.all_rows.length
      ? "当前筛选条件下没有计划；请清空部分筛选条件。"
      : "尚未采集到计划。请先选择正确账户并同步千川计划列表。";
    body.replaceChildren();
    renderPromotionPlanSelection();
    return;
  }
  body.replaceChildren(...view.rows.slice(0, 200).map((row) => {
    const tr = document.createElement("tr");
    tr.dataset.planKey = row.plan_key;
    tr.dataset.risk = row.risk_level || "info";
    const selectCell = document.createElement("td");
    selectCell.className = "promotion-plan-select-cell";
    const select = document.createElement("input");
    select.type = "checkbox";
    select.value = row.plan_key;
    select.dataset.promotionPlanSelect = "true";
    select.setAttribute("aria-label", `选择计划 ${row.plan_name}`);
    selectCell.append(select);
    const statusCell = document.createElement("td");
    const status = document.createElement("span");
    status.className = `plan-status ${row.delivery_status === "投放中" ? "running" : row.delivery_status === "暂停" ? "paused" : ""}`;
    status.textContent = row.delivery_status;
    statusCell.append(status);
    const metric = promotionPlanCell(
      `${promotionPlanMoney(row.spend)} / ${row.roi === null ? "--" : Number(row.roi).toFixed(2)}`,
      `${row.orders === null ? "成交待确认" : `${row.orders} 单`} · 预算 ${promotionPlanMoney(row.budget)}`,
    );
    tr.append(
      selectCell,
      promotionPlanCell(row.plan_name, `${row.plan_id || "计划 ID 缺失"} · 质量 ${row.quality_score} · ${row.data_age_label}`),
      promotionPlanCell(row.account_label, `${promotionPlanModeLabel(row.promotion_mode)} · ${promotionPlanTypeLabel(row.plan_type)}`),
      statusCell,
      metric,
      promotionPlanCapabilitiesCell(row),
      promotionPlanActionCell(row),
    );
    return tr;
  }));
  renderPromotionPlanSelection();
}

async function persistPromotionPlanFilters() {
  await chrome.storage.local.set({ [PROMOTION_PLAN_FILTERS_KEY]: currentPromotionPlanFilters });
}

function readPromotionPlanFilters() {
  currentPromotionPlanFilters = globalThis.DianPromotionPlanCenter.normalizeFilters({
    account: document.getElementById("promotion-plan-account-filter").value,
    mode: document.getElementById("promotion-plan-mode-filter").value,
    plan_type: document.getElementById("promotion-plan-type-filter").value,
    status: document.getElementById("promotion-plan-status-filter").value,
    binding: document.getElementById("promotion-plan-binding-filter").value,
    query: document.getElementById("promotion-plan-search").value,
    features: [...document.querySelectorAll("#promotion-plan-feature-filters input:checked")].map((input) => input.value),
  });
  renderPromotionPlanConsole();
  persistPromotionPlanFilters().catch(() => undefined);
}

async function refreshPromotionPlanConsole({ syncPage = false, planType = "all" } = {}) {
  const state = document.getElementById("promotion-plan-center-state");
  state.className = "";
  state.textContent = syncPage ? "正在同步千川页" : "正在刷新计划";
  const normalizedPlanType = String(planType || "all").trim().toLowerCase();
  const expectedPageTypes = normalizedPlanType === "live"
    ? ["qianchuan_live"]
    : normalizedPlanType === "product"
      ? ["campaigns", "qianchuan_campaigns"]
      : ["campaigns", "qianchuan_campaigns", "qianchuan_live"];
  const purpose = normalizedPlanType === "live"
    ? "直播计划只读同步"
    : normalizedPlanType === "product"
      ? "商品计划只读同步"
      : "投放计划只读同步";
  if (syncPage) await syncRecentQianchuanPage({
    expectedPageTypes,
    purpose,
  });
  const payload = await bridgeFetch("/qianchuan/plan-console");
  renderPromotionPlanConsole(payload);
  return payload;
}

async function togglePromotionPlanBinding(planKey, action) {
  const plan = currentPromotionPlanView?.all_rows?.find((item) => item.plan_key === planKey);
  if (!plan) return;
  if (action === "unbind") {
    const next = { ...currentPromotionPlanBindings };
    const removed = next[planKey];
    delete next[planKey];
    currentPromotionPlanBindings = next;
    await chrome.storage.local.set({ [PROMOTION_PLAN_BINDINGS_KEY]: currentPromotionPlanBindings });
    renderPromotionPlanConsole();
    await recordAutopilotActivity("binding", `解除本地绑定 · ${plan.plan_name}`, `${removed?.profile_label || "本地策略"}已移除；未修改千川。`);
    return;
  }
  const profile = document.getElementById("promotion-plan-binding-profile").value;
  const binding = globalThis.DianPromotionPlanCenter.createBinding(plan, { profile });
  currentPromotionPlanBindings = { ...currentPromotionPlanBindings, [planKey]: binding };
  await chrome.storage.local.set({ [PROMOTION_PLAN_BINDINGS_KEY]: currentPromotionPlanBindings });
  renderPromotionPlanConsole();
  await recordAutopilotActivity("binding", `建立本地绑定 · ${plan.plan_name}`, `${binding.profile_label}；仅保存本地规则，平台写入关闭。`);
}

function renderPromotionBatchDraft(draft = currentPromotionBatchDraft) {
  const result = document.getElementById("batch-plan-result");
  const copy = document.getElementById("batch-plan-copy");
  if (!draft?.items?.length) {
    result.className = "batch-plan-result empty-state";
    result.textContent = "尚未生成批量计划草稿。";
    copy.disabled = true;
    return;
  }
  result.className = "batch-plan-result";
  result.replaceChildren(...draft.items.map((item) => {
    const row = document.createElement("article");
    const title = document.createElement("strong"); title.textContent = item.plan_name;
    const metric = document.createElement("span"); metric.textContent = `${promotionPlanMoney(item.budget)} · ROI ${Number(item.roi_target).toFixed(2)} · 对象 ${item.target_id}`;
    const tag = document.createElement("em"); tag.textContent = "本地草稿";
    row.append(title, metric, tag);
    return row;
  }));
  document.getElementById("batch-plan-review-summary").textContent = `${draft.mode_label} · ${draft.plan_type_label} · ${draft.items.length} 个草稿 · 自动提交关闭`;
  copy.disabled = false;
}

async function generatePromotionBatchDraft() {
  const draft = globalThis.DianPromotionPlanCenter.createBatchDraft({
    account_key: document.getElementById("batch-plan-account").value,
    mode: currentBatchPlanMode,
    plan_type: document.getElementById("batch-plan-type").value,
    target_ids: document.getElementById("batch-plan-targets").value,
    name_prefix: document.getElementById("batch-plan-name").value,
    budget: document.getElementById("batch-plan-budget").value,
    roi_target: document.getElementById("batch-plan-roi").value,
    long_term: document.getElementById("batch-plan-long-term").checked,
    star_material: document.getElementById("batch-plan-star-material").checked,
    smart_coupon: document.getElementById("batch-plan-smart-coupon").checked,
  });
  currentPromotionBatchDraft = draft;
  currentPromotionBatchDrafts = [draft, ...currentPromotionBatchDrafts.filter((item) => item.batch_id !== draft.batch_id)].slice(0, 20);
  await chrome.storage.local.set({ [PROMOTION_BATCH_DRAFTS_KEY]: currentPromotionBatchDrafts });
  renderPromotionBatchDraft(draft);
  await recordAutopilotActivity("batch_draft", `生成${draft.mode_label}${draft.plan_type_label}批量草稿`, `${draft.items.length} 个对象 · 本地草稿 · 未调用平台写接口。`);
}

function formatAutopilotMoney(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `¥${number.toFixed(2)}` : "待确认";
}

function formatAutopilotDelta(candidate = {}) {
  if (candidate.kind === "pause") return "等待人工确认";
  if (candidate.current_value === null || candidate.target_value === null) return "数值待复核";
  const pct = candidate.delta_pct === null ? "" : ` · ${candidate.delta_pct >= 0 ? "+" : ""}${Math.round(candidate.delta_pct * 100)}%`;
  return `${formatAutopilotMoney(candidate.current_value)} → ${formatAutopilotMoney(candidate.target_value)}${pct}`;
}

function autopilotScopeKey(view = currentAutopilotCenterView || {}) {
  return globalThis.DianAutopilotCenter.configFingerprint({
    store_key: String(view.catalog?.selected_store_key || ""),
    account_key: String(view.catalog?.selected_account_key || ""),
  });
}

function renderAutopilotPackage(preview = currentAutopilotPackagePreview || {}) {
  const packageValue = preview.package || {};
  document.getElementById("autopilot-package-name").textContent = `${packageValue.package_name || "守钱 · 当前店铺"} v${packageValue.revision || 1}`;
  document.getElementById("autopilot-package-evidence").textContent = preview.evidence_label || "经营建档与人工边界";
  document.getElementById("autopilot-package-plans").textContent = `${preview.affected_plan_count || 0} 个匿名计划`;
  document.getElementById("autopilot-package-change-count").textContent = `${preview.changed_count || 0} 项`;
  const status = document.getElementById("autopilot-package-status");
  status.className = preview.already_applied ? "safe" : preview.can_apply ? "" : "danger";
  status.textContent = preview.already_applied ? "本机已保存" : preview.can_apply ? `${preview.changed_count} 项待保存` : "暂不可保存";
  const blockers = document.getElementById("autopilot-package-blockers");
  if (preview.blockers?.length) {
    blockers.className = "autopilot-package-blockers";
    blockers.textContent = preview.blockers.join("；");
  } else {
    blockers.className = "autopilot-package-blockers safe";
    blockers.textContent = preview.already_applied
      ? "当前预览与本机已保存策略包一致。"
      : "作用域和技术护栏已齐备，可以保存为本机只读策略包。";
  }
  const changes = document.getElementById("autopilot-package-changes");
  const changedItems = (preview.changes || []).filter((item) => item.changed);
  if (!changedItems.length) {
    changes.className = "autopilot-package-changes empty-state";
    changes.textContent = preview.already_applied ? "当前没有配置漂移。" : "完成经营建档后显示新旧配置差异。";
  } else {
    changes.className = "autopilot-package-changes";
    changes.replaceChildren(...changedItems.map((item) => {
      const row = document.createElement("div");
      row.className = "autopilot-package-change changed";
      const label = document.createElement("strong"); label.textContent = item.label;
      const after = document.createElement("span"); after.textContent = item.after;
      const before = document.createElement("small"); before.textContent = `原配置：${item.before}`;
      row.append(label, after, before);
      return row;
    }));
  }
  document.getElementById("autopilot-package-notice").textContent = preview.notice || "策略包只保存到本机。";
  const apply = document.getElementById("autopilot-package-apply");
  apply.disabled = preview.can_apply !== true;
  apply.textContent = preview.overwrite_warning ? "确认更新本机策略包" : preview.already_applied ? "策略包已是最新" : "保存策略包到本机";
}

function renderAutopilotOperatingLoop(business = {}, recovery = {}) {
  const status = document.getElementById("autopilot-recovery-status");
  status.className = recovery.tone || "warning";
  status.textContent = recovery.state_label || "等待经营建档";
  document.getElementById("autopilot-business-stage").textContent = business.stage_label || "经营阶段待确认";
  document.getElementById("autopilot-business-weights").textContent = business.gmv_weight === null || business.gmv_weight === undefined
    ? "目标权重待计算"
    : `成交 ${business.gmv_weight}% · 利润 ${business.profit_weight}%`;
  document.getElementById("autopilot-business-roi").textContent = business.break_even_roi === null || business.break_even_roi === undefined
    || business.observation_roi_floor === null || business.observation_roi_floor === undefined
    ? "待计算"
    : `${Number(business.break_even_roi).toFixed(2)} / ${Number(business.observation_roi_floor).toFixed(2)}`;
  document.getElementById("autopilot-business-attribution").textContent = business.attribution_label || "归因占比待补齐";
  document.getElementById("autopilot-recovery-boundary").textContent = recovery.recheck_minutes === null || recovery.recheck_minutes === undefined
    ? "待配置"
    : `冷却 ${recovery.recheck_minutes} 分 · 最多 ${recovery.max_cycles} 轮`;
  const blockers = document.getElementById("autopilot-recovery-blockers");
  if (recovery.blockers?.length) {
    blockers.className = "autopilot-recovery-blockers danger";
    blockers.textContent = recovery.blockers.join("；");
  } else if (recovery.warnings?.length) {
    blockers.className = "autopilot-recovery-blockers";
    blockers.textContent = recovery.warnings.join("；");
  } else {
    blockers.className = "autopilot-recovery-blockers safe";
    blockers.textContent = "经营阶段、利润线、归因与恢复护栏已齐备；当前仍只生成本机候选。";
  }
  const stepLabels = { complete: "已完成", current: "当前步骤", failed: "已锁定", waiting: "等待" };
  document.getElementById("autopilot-recovery-steps").replaceChildren(...(recovery.steps || []).map((item, index) => {
    const step = document.createElement("div");
    step.className = item.state || "waiting";
    const title = document.createElement("strong"); title.textContent = `${index + 1}. ${item.label}`;
    const detail = document.createElement("span"); detail.textContent = stepLabels[item.state] || "等待";
    step.append(title, detail);
    return step;
  }));
  document.getElementById("autopilot-recovery-rule").textContent = `${recovery.rule_summary || "关键阈值待补齐。"}；${recovery.decision || "不会使用猜测值执行。"}`;
}

function renderAutopilotRealtimeTask(task = {}) {
  const status = document.getElementById("autopilot-realtime-status");
  const failed = task.platform_write_attempted === true || ["failed", "readback_failed", "rejected", "expired"].includes(task.state);
  status.className = failed ? "danger" : "";
  status.textContent = task.state_label || "等待候选";
  const stepLabels = { complete: "已完成", current: "当前步骤", failed: "已阻止", waiting: "等待" };
  document.getElementById("autopilot-realtime-steps").replaceChildren(...(task.steps || []).map((item, index) => {
    const step = document.createElement("div");
    step.className = item.state || "waiting";
    const title = document.createElement("strong"); title.textContent = `${index + 1}. ${item.label}`;
    const detail = document.createElement("span"); detail.textContent = stepLabels[item.state] || "等待";
    step.append(title, detail);
    return step;
  }));
  document.getElementById("autopilot-realtime-note").textContent = `${task.plan_label || "尚无计划任务"} · ${task.note || "等待首次影子评估。"}`;
}

const CONTROL_TASK_BLOCKER_LABELS = Object.freeze({
  PRODUCTION_WRITE_DISABLED: "真实写入关闭",
  ACCOUNT_WRITE_CONTRACT_UNVERIFIED: "当前账户写合同未验真",
  FIRST_RELEASE_DECREASE_ONLY: "首发版只允许降预算",
  STATUS_WRITE_CONTRACT_NOT_ACCOUNT_VERIFIED: "启停合同尚未完成账户验真",
  CLOSE_SEMANTICS_UNVERIFIED: "结束动作不会猜测映射成删除",
  DURATION_WRITE_CONTRACT_UNVERIFIED: "时长写合同尚未验真",
  CURRENT_STATUS_UNVERIFIED: "当前状态无法确认",
  CURRENT_STATUS_NOT_ACTIONABLE: "当前状态不可直接操作",
  CLOSED_TASK_IS_TERMINAL: "已结束任务不可再次操作",
  NO_EFFECT_CHANGE: "新旧值没有变化",
  POSITIVE_NUMERIC_VALUE_REQUIRED: "需要有效的正数",
  DURATION_MINUTES_MUST_BE_INTEGER: "时长必须为整数分钟",
  TARGET_MUST_BE_LOWER: "目标值必须更低",
  TARGET_MUST_BE_HIGHER: "目标值必须更高",
});

function controlTaskReasonText(task = {}) {
  const reasons = (task.reason_codes || []).map((code) => CHENGFANG_DECISION_REASONS[code] || CONTROL_TASK_BLOCKER_LABELS[code] || code);
  const blockers = (task.blockers || []).map((code) => CONTROL_TASK_BLOCKER_LABELS[code] || code);
  if (blockers.length) return `已阻止：${blockers.join("；")}`;
  if (reasons.length) return `依据：${reasons.join("；")}`;
  if (task.state === "verified") return "模拟值与回读值一致；这不代表千川已被修改。";
  return "等待人工核对当前值、目标值和证据时间。";
}

function renderControlTaskCenter(summaryValue = currentControlTaskSummary) {
  const policy = globalThis.DianControlTaskCenter;
  const root = document.getElementById("control-task-center");
  if (!policy || !root) return;
  const summary = summaryValue && typeof summaryValue === "object" ? summaryValue : {
    families: [], tasks: [], importable_candidates: [], platform_write_enabled: false,
    notice: "控制任务中心暂时无法读取，请确认本地 Agent 已更新。",
  };
  currentControlTaskSummary = summary;
  const view = policy.deriveView(summary, currentControlTaskFamily);
  currentControlTaskView = view;
  currentControlTaskFamily = view.selected_family;

  const status = document.getElementById("control-task-center-status");
  status.className = view.safe ? (view.all_task_count ? "safe" : "warning") : "danger";
  status.textContent = view.safe
    ? view.all_task_count ? `${view.pending_count} 待处理 · ${view.verified_count} 已通过` : "本机演练待启动"
    : "安全门已阻止";
  document.querySelectorAll("[data-control-task-family]").forEach((button) => {
    const family = view.families.find((item) => item.id === button.dataset.controlTaskFamily);
    if (!family) return;
    button.classList.toggle("active", family.selected);
    button.setAttribute("aria-selected", String(family.selected));
    button.title = `${family.description} ${family.contract_note}`;
    const counter = document.getElementById(`control-task-${family.id}-count`);
    if (counter) counter.textContent = String(family.task_count || 0);
  });
  document.getElementById("control-task-family-note").textContent = `${view.selected_definition.description} ${view.selected_definition.contract_note}`;
  document.getElementById("control-task-total").textContent = String(view.all_task_count);
  document.getElementById("control-task-pending").textContent = String(view.pending_count);
  document.getElementById("control-task-verified").textContent = String(view.verified_count);
  document.getElementById("control-task-write").textContent = "关闭";
  const builder = document.getElementById("control-task-builder");
  builder.hidden = !view.manual_rehearsal_available;
  if (!builder.hidden) {
    document.getElementById("control-task-builder-title").textContent = `${view.selected_definition.label}演练`;
    document.querySelectorAll("[data-control-task-builder]").forEach((section) => {
      section.hidden = section.dataset.controlTaskBuilder !== view.selected_family;
    });
    document.getElementById("control-task-builder-create").textContent = `生成${view.selected_definition.short_label}演练草稿`;
    document.getElementById("control-task-builder-note").textContent = view.selected_family === "status"
      ? "必须先人工核对当前状态；“结束”只作为本机目标，不会映射为删除。"
      : "时长单位固定为分钟；手工值只能用于本机演练，不能进入生产写入。";
  } else {
    builder.open = false;
  }

  const list = document.getElementById("control-task-list");
  if (!view.tasks.length) {
    list.className = "control-task-list empty-state";
    list.textContent = view.selected_family === "status"
      ? "当前没有状态控制任务。同步任务当前状态后，再生成启用、暂停或结束演练。"
      : view.selected_family === "duration"
        ? "当前没有时长控制任务。先取得任务当前投放窗口；不会猜测时间单位或结束时间。"
        : view.importable_candidates.length
          ? "已有可导入的降预算候选，点击下方按钮生成单变量控制任务。"
          : "当前没有预算控制任务。先由 A1 影子判断生成候选。";
  } else {
    list.className = "control-task-list";
    list.replaceChildren(...view.tasks.map((task) => {
      const card = document.createElement("article");
      card.className = `control-task-item ${task.tone}`;
      const head = document.createElement("header");
      const title = document.createElement("strong"); title.textContent = `${task.operation_label} · ${task.task_label}`;
      const badge = document.createElement("b"); badge.textContent = task.state_label;
      const time = document.createElement("small");
      time.textContent = task.updated_at ? `更新于 ${new Date(task.updated_at).toLocaleString()} · 单变量任务` : "单变量任务";
      head.append(title, badge, time);
      const change = document.createElement("div"); change.className = "control-task-change";
      const before = document.createElement("div");
      const beforeLabel = document.createElement("span"); beforeLabel.textContent = "当前值";
      const beforeValue = document.createElement("strong"); beforeValue.textContent = task.current_label;
      before.append(beforeLabel, beforeValue);
      const arrow = document.createElement("b"); arrow.textContent = "→";
      const after = document.createElement("div");
      const afterLabel = document.createElement("span"); afterLabel.textContent = "目标值";
      const afterValue = document.createElement("strong"); afterValue.textContent = task.target_label;
      after.append(afterLabel, afterValue);
      change.append(before, arrow, after);
      const reason = document.createElement("p"); reason.className = "control-task-reason"; reason.textContent = controlTaskReasonText(task);
      const footer = document.createElement("footer");
      task.actions.forEach((action) => {
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.controlTaskAction = action.id;
        button.dataset.taskId = task.task_id;
        markAgentWriteControl(button);
        button.textContent = action.label;
        if (action.primary) button.classList.add("primary");
        if (action.id === "reject") button.classList.add("danger");
        footer.append(button);
      });
      card.append(head, change, reason);
      if (task.actions.length) card.append(footer);
      return card;
    }));
  }

  document.getElementById("control-task-next").textContent = `${view.primary_action.label}：${view.primary_action.detail}`;
  document.getElementById("control-task-contract").textContent = view.selected_definition.contract_note;
  document.getElementById("control-task-safety").textContent = `重要：${view.notice} “结束”不会自动映射为“删除”，三类动作也不会合并提交。`;
  const primary = document.getElementById("control-task-primary");
  primary.dataset.action = view.primary_action.id;
  primary.dataset.candidateId = view.primary_action.candidate_id || "";
  primary.textContent = view.primary_action.label;
  primary.disabled = view.primary_action.enabled === false || !view.safe;
}

function applyControlTaskResponse(result = {}) {
  if (result.control_tasks && typeof result.control_tasks === "object") currentControlTaskSummary = result.control_tasks;
  renderControlTaskCenter(currentControlTaskSummary);
}

const SCHEDULE_BLOCKER_LABELS = Object.freeze({
  TOO_MANY_TIME_RANGES: "每天最多配置 10 个时间段",
  TIME_RANGES_OVERLAP: "时间段存在重叠，请先拆开",
  AT_LEAST_ONE_TIME_RANGE_REQUIRED: "启用时至少保留一个时间段",
  ENABLED_BOOLEAN_REQUIRED: "启用状态格式无效",
  SCOPE_BINDING_INCOMPLETE: "当前店铺与账户作用域尚未绑定",
  PLAN_SCOPE_REQUIRED: "尚未绑定当前计划",
});

function scheduleBlockerText(code = "") {
  if (SCHEDULE_BLOCKER_LABELS[code]) return SCHEDULE_BLOCKER_LABELS[code];
  if (/^TIME_RANGE_\d+_INVALID$/.test(code)) return `第 ${code.split("_")[2]} 个时间段格式无效`;
  if (/^TIME_RANGE_\d+_EMPTY$/.test(code)) return `第 ${code.split("_")[2]} 个时间段起止时间相同`;
  if (/^TIME_RANGE_\d+_CROSS_DAY_MUST_SPLIT$/.test(code)) return `第 ${code.split("_")[2]} 个时间段跨天，请拆成两段`;
  return code || "时间配置无效";
}

function validateScheduleDraft() {
  const blockers = [];
  const normalized = scheduleDraftRanges.map((item, index) => {
    const start = String(item?.start || "").trim();
    const end = String(item?.end || "").trim();
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(start) || !/^([01]\d|2[0-3]):[0-5]\d$/.test(end)) {
      blockers.push(`第 ${index + 1} 个时间段需要完整填写`);
      return { start, end, startMinute: null, endMinute: null };
    }
    const toMinute = (value) => Number(value.slice(0, 2)) * 60 + Number(value.slice(3));
    const startMinute = toMinute(start);
    const endMinute = toMinute(end);
    if (startMinute === endMinute) blockers.push(`第 ${index + 1} 个时间段起止时间不能相同`);
    if (startMinute > endMinute) blockers.push(`第 ${index + 1} 个时间段跨天，请拆成 00:00 前后两段`);
    return { start, end, startMinute, endMinute };
  });
  if (scheduleDraftRanges.length > 10) blockers.push("每天最多配置 10 个时间段");
  if (scheduleDraftEnabled && !scheduleDraftRanges.length) blockers.push("启用时至少保留一个时间段");
  const sortable = normalized.filter((item) => item.startMinute !== null && item.startMinute < item.endMinute)
    .sort((left, right) => left.startMinute - right.startMinute || left.endMinute - right.endMinute);
  for (let index = 1; index < sortable.length; index += 1) {
    if (sortable[index].startMinute < sortable[index - 1].endMinute) {
      blockers.push("时间段存在重叠，请先拆开");
      break;
    }
  }
  return { valid: blockers.length === 0, blockers: [...new Set(blockers)] };
}

function hydrateScheduleDraft(summary = {}, force = false) {
  if (scheduleDraftHydrated && !force) return;
  const config = summary.active || summary.latest;
  if (config && typeof config === "object") {
    scheduleDraftEnabled = config.enabled === true;
    scheduleDraftRanges = Array.isArray(config.time_ranges)
      ? config.time_ranges.map((item) => ({ start: String(item.start || ""), end: String(item.end || "") })).slice(0, 10)
      : [];
  }
  scheduleDraftHydrated = true;
}

function renderAutomationPolicyMatrix() {
  const policy = globalThis.DianAutomationPolicyCenter;
  const root = document.getElementById("automation-policy-matrix");
  if (!policy || !root) return;
  const matrix = policy.derivePolicyMatrix({
    schedule: currentScheduleControl || {},
    runtime: currentChengfangAgentRuntime || {},
  });
  root.replaceChildren(...matrix.map((item) => {
    const card = document.createElement("article");
    card.className = `automation-policy-card ${item.tone || "muted"}`;
    const header = document.createElement("header");
    const stage = document.createElement("span"); stage.textContent = `第 ${item.stage} 级`;
    const state = document.createElement("b"); state.textContent = item.state_label;
    header.append(stage, state);
    const title = document.createElement("strong"); title.textContent = item.label;
    const description = document.createElement("p"); description.textContent = item.description;
    const evidence = document.createElement("small"); evidence.textContent = `准入证据：${item.evidence}`;
    const next = document.createElement("footer"); next.textContent = `下一步：${item.next_step}`;
    card.append(header, title, description, evidence, next);
    return card;
  }));
  const usable = matrix.filter((item) => ["available", "in_progress", "local_verified", "shadow_available", "design_ready"].includes(item.state)).length;
  const status = document.getElementById("automation-policy-status");
  status.className = currentScheduleView?.unsafe ? "danger" : currentScheduleView?.active ? "safe" : "warning";
  status.textContent = `${usable} / ${matrix.length} 级可推进`;
}

function renderScheduleRanges() {
  const list = document.getElementById("schedule-range-list");
  if (!list) return;
  if (!scheduleDraftRanges.length) {
    list.className = "schedule-range-list empty-state";
    list.textContent = scheduleDraftEnabled ? "启用状态下必须添加至少一个时间段。" : "当前配置为关闭；如需启用，请先添加每日时间段。";
  } else {
    list.className = "schedule-range-list";
    list.replaceChildren(...scheduleDraftRanges.map((item, index) => {
      const row = document.createElement("div"); row.className = "schedule-range-row";
      const order = document.createElement("b"); order.textContent = String(index + 1).padStart(2, "0");
      const startLabel = document.createElement("label");
      const startText = document.createElement("span"); startText.textContent = "启用";
      const start = document.createElement("input"); start.type = "time"; start.value = item.start; start.dataset.scheduleIndex = String(index); start.dataset.scheduleField = "start"; start.setAttribute("aria-label", `第 ${index + 1} 段启用时间`);
      startLabel.append(startText, start);
      const arrow = document.createElement("span"); arrow.className = "schedule-range-arrow"; arrow.textContent = "→";
      const endLabel = document.createElement("label");
      const endText = document.createElement("span"); endText.textContent = "暂停";
      const end = document.createElement("input"); end.type = "time"; end.value = item.end; end.dataset.scheduleIndex = String(index); end.dataset.scheduleField = "end"; end.setAttribute("aria-label", `第 ${index + 1} 段暂停时间`);
      endLabel.append(endText, end);
      const remove = document.createElement("button"); remove.type = "button"; remove.dataset.scheduleRemove = String(index); remove.textContent = "移除"; remove.setAttribute("aria-label", `移除第 ${index + 1} 个时间段`);
      row.append(order, startLabel, arrow, endLabel, remove);
      return row;
    }));
  }
  document.getElementById("schedule-range-add").disabled = scheduleDraftRanges.length >= 10;
  const validation = validateScheduleDraft();
  const validationNode = document.getElementById("schedule-control-validation");
  validationNode.className = `schedule-control-validation ${validation.valid ? "safe" : "danger"}`;
  validationNode.textContent = validation.valid
    ? scheduleDraftEnabled ? `${scheduleDraftRanges.length} 个时间段校验通过；保存后仍需人工复核。` : "当前为关闭配置；保存后不会生成定时事件。"
    : validation.blockers.join("；");
  const save = document.getElementById("schedule-control-save");
  save.disabled = !validation.valid || currentScheduleView?.scope_bound !== true || currentScheduleView?.unsafe === true;
}

function formatScheduleEventTime(value) {
  const timestamp = Number(value);
  if (!Number.isFinite(timestamp)) return "时间待确认";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(new Date(timestamp));
}

function renderScheduleControl(summaryValue = currentScheduleControl, options = {}) {
  const policy = globalThis.DianAutomationPolicyCenter;
  const root = document.getElementById("automation-policy-center");
  if (!policy || !root) return;
  const summary = summaryValue && typeof summaryValue === "object" ? summaryValue : {
    scope_bound: false, platform_write_enabled: false, production_scheduler_enabled: false,
    notice: "定时启停暂时无法读取，请确认本地 Agent 已更新。",
  };
  currentScheduleControl = summary;
  currentScheduleView = policy.normalizeSchedule(summary);
  hydrateScheduleDraft(summary, options.forceHydrate === true);
  document.getElementById("schedule-control-enabled").checked = scheduleDraftEnabled;
  const state = document.getElementById("schedule-control-state");
  state.className = currentScheduleView.tone || "warning";
  state.textContent = currentScheduleView.unsafe
    ? "安全门已阻止"
    : currentScheduleView.scope_bound ? currentScheduleView.state_label : "等待绑定计划";
  document.getElementById("schedule-control-notice").textContent = currentScheduleView.notice;

  const latest = currentScheduleView.latest;
  const revision = document.getElementById("schedule-control-revision");
  if (!latest) {
    revision.className = "schedule-control-revision empty-state";
    revision.textContent = currentScheduleView.scope_bound ? "尚未保存定时启停草稿。" : "先同步并绑定当前乘方计划，再配置每日时间段。";
  } else {
    revision.className = `schedule-control-revision ${currentScheduleView.tone || "warning"}`;
    const header = document.createElement("header");
    const title = document.createElement("strong"); title.textContent = `配置 v${latest.revision || 1} · ${currentScheduleView.state_label}`;
    const badge = document.createElement("b"); badge.textContent = latest.enabled ? "每日时段开启" : "每日时段关闭";
    header.append(title, badge);
    const summaryText = document.createElement("p");
    const ranges = Array.isArray(latest.time_ranges) ? latest.time_ranges : [];
    summaryText.textContent = ranges.length ? ranges.map((item) => `${item.start}–${item.end}`).join("、") : "未配置启用时段";
    const meta = document.createElement("small");
    meta.textContent = `${latest.changed_count || 0} 项变化 · Asia/Shanghai · 单计划 · 平台写入关闭`;
    const actions = document.createElement("footer");
    currentScheduleView.actions.forEach((action) => {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.scheduleAction = action.id;
      button.dataset.revisionId = currentScheduleView.revision_id;
      markAgentWriteControl(button);
      button.textContent = action.label;
      if (action.primary) button.classList.add("primary");
      if (action.id === "reject") button.classList.add("danger");
      actions.append(button);
    });
    revision.replaceChildren(header, summaryText, meta);
    if (currentScheduleView.actions.length) revision.append(actions);
  }

  const events = currentScheduleView.next_events || [];
  document.getElementById("schedule-event-count").textContent = `${events.length} 项`;
  const eventList = document.getElementById("schedule-event-list");
  if (!events.length) {
    eventList.className = "schedule-event-list empty-state";
    eventList.textContent = currentScheduleView.active?.enabled === false
      ? "当前定时启停已关闭，不会生成未来事件。"
      : "完成本机模拟并回读后，显示未来 48 小时启用与暂停事件。";
  } else {
    eventList.className = "schedule-event-list";
    eventList.replaceChildren(...events.map((event) => {
      const row = document.createElement("div");
      row.className = event.operation === "ENABLE" ? "enable" : "pause";
      const operation = document.createElement("b"); operation.textContent = event.operation_label || event.operation;
      const time = document.createElement("strong"); time.textContent = formatScheduleEventTime(event.scheduled_at_ms);
      const note = document.createElement("small"); note.textContent = "本机预演 · 不提交千川";
      row.append(operation, time, note);
      return row;
    }));
  }
  const nextText = currentScheduleView.actions[0]?.label
    || (currentScheduleView.active ? "本机配置已验证；等待下一时段事件预览" : "保存草稿 → 人工复核 → 48 小时模拟 → 模拟回读");
  document.getElementById("schedule-control-next").textContent = nextText;
  renderScheduleRanges();
  renderAutomationPolicyMatrix();
}

function applyScheduleControlResponse(result = {}) {
  if (result.schedule_control && typeof result.schedule_control === "object") currentScheduleControl = result.schedule_control;
  renderScheduleControl(currentScheduleControl, { forceHydrate: true });
}

const AUTOPILOT_ACTIVITY_LABELS = Object.freeze({ config: "策略", sync: "同步", shadow: "影子", review: "复核", control: "控制" });

function renderAutopilotActivity() {
  const list = document.getElementById("autopilot-activity-list");
  if (!list) return;
  const type = document.getElementById("autopilot-activity-filter")?.value || "all";
  const keyword = String(document.getElementById("autopilot-activity-search")?.value || "").trim().toLowerCase();
  const normalized = currentAutopilotActivity.filter((item) => item && typeof item === "object");
  document.getElementById("autopilot-activity-count").textContent = `${normalized.length} 条本机记录`;
  const filtered = normalized.filter((item) => {
    if (type !== "all" && item.type !== type) return false;
    return !keyword || `${item.title || ""} ${item.detail || ""}`.toLowerCase().includes(keyword);
  }).slice(0, 20);
  if (!filtered.length) {
    list.className = "autopilot-activity-list empty-state";
    list.textContent = normalized.length ? "当前筛选条件没有记录。" : "尚无托管操作记录。";
    return;
  }
  list.className = "autopilot-activity-list";
  list.replaceChildren(...filtered.map((item) => {
    const row = document.createElement("div"); row.className = "autopilot-activity-item";
    const typeLabel = document.createElement("b"); typeLabel.textContent = AUTOPILOT_ACTIVITY_LABELS[item.type] || "本机";
    const title = document.createElement("strong"); title.textContent = item.title || "托管操作";
    const time = document.createElement("time"); time.textContent = new Date(Number(item.created_at || 0)).toLocaleString();
    const detail = document.createElement("small"); detail.textContent = item.detail || "未触发平台写入。";
    row.append(typeLabel, title, time, detail);
    return row;
  }));
}

async function recordAutopilotActivity(type, title, detail) {
  const entry = {
    id: `${Date.now()}-${String(type || "local")}`,
    type: AUTOPILOT_ACTIVITY_LABELS[type] ? type : "review",
    title: String(title || "托管操作").slice(0, 120),
    detail: String(detail || "").slice(0, 300),
    created_at: Date.now(),
    local_only: true,
    platform_write_attempted: false,
  };
  currentAutopilotActivity = [entry, ...currentAutopilotActivity.filter((item) => item?.id !== entry.id)].slice(0, 60);
  await chrome.storage.local.set({ [AUTOPILOT_ACTIVITY_KEY]: currentAutopilotActivity });
  renderAutopilotActivity();
  return entry;
}

function renderAutopilotCenter() {
  const policy = globalThis.DianAutopilotCenter;
  const root = document.getElementById("autopilot-center");
  if (!policy || !root) return;
  const view = policy.deriveCenterView({
    settings: currentAutopilotCenterSettings,
    gate: currentChengfangGate,
    runtime: currentChengfangAgentRuntime || {},
    pilot: currentChengfangA2Pilot || currentChengfangAgentRuntime?.a2_pilot || {},
    catalog: currentQianchuanCatalog || {},
    candidatePath: currentChengfangCandidatePath,
    localPlan: currentChengfangLocalPlan,
    writeEnabled: false,
  });
  currentAutopilotCenterView = view;
  currentAutopilotCenterSettings = view.settings;
  const scopeKey = autopilotScopeKey(view);
  currentAutopilotPackagePreview = policy.strategyPackagePreview({
    view,
    gate: currentChengfangGate,
    runtime: currentChengfangAgentRuntime || {},
    pilot: currentChengfangA2Pilot || currentChengfangAgentRuntime?.a2_pilot || {},
    localPlan: currentChengfangLocalPlan,
    appliedPackage: currentAutopilotPackages[scopeKey] || null,
  });

  const status = document.getElementById("autopilot-center-status");
  status.className = view.status.tone;
  status.textContent = view.status.label;
  status.title = view.status.detail;
  document.getElementById("autopilot-center-mode-note").textContent = view.mode_note;
  document.getElementById("autopilot-center-store").textContent = view.catalog.store_label;
  document.getElementById("autopilot-center-source").textContent = `${view.catalog.store_count} 个店铺 · ${view.catalog.source_label}`;
  document.getElementById("autopilot-center-account").textContent = view.catalog.account_label;
  document.getElementById("autopilot-center-shadow").textContent = view.shadow.label;
  document.getElementById("autopilot-center-write").textContent = view.production_write_label;
  document.getElementById("autopilot-center-safety").textContent = `重要：${view.safety_notice}`;

  document.querySelectorAll("[data-autopilot-mode]").forEach((button) => {
    const mode = view.modes.find((item) => item.id === button.dataset.autopilotMode);
    if (!mode) return;
    button.classList.toggle("active", mode.selected);
    button.disabled = mode.locked;
    button.setAttribute("aria-pressed", String(mode.selected));
    button.title = mode.locked ? mode.locked_reason : mode.description;
    const badge = button.querySelector("b");
    if (badge) badge.textContent = mode.selected ? "当前" : mode.locked ? "待解锁" : "可选择";
  });

  const drafts = document.getElementById("autopilot-center-drafts");
  document.getElementById("autopilot-center-draft-count").textContent = `${view.draft_count} 条`;
  if (!view.drafts.length) {
    drafts.className = "autopilot-center-drafts empty-state";
    drafts.textContent = view.running
      ? "影子托管正在观察；当前没有触发确定性边界，保持不动。"
      : "完成账户绑定和数据校验后，这里会汇总最多 3 条待复核草稿。";
  } else {
    drafts.className = "autopilot-center-drafts";
    drafts.replaceChildren(...view.drafts.map((candidate) => {
      const card = document.createElement("article");
      card.className = `autopilot-draft-item ${candidate.kind}`;
      const title = document.createElement("strong");
      title.textContent = `${candidate.kind_label} · ${candidate.plan_label}`;
      const value = document.createElement("b");
      value.textContent = formatAutopilotDelta(candidate);
      const reason = document.createElement("small");
      const reasonText = candidate.reasons.map((code) => CHENGFANG_DECISION_REASONS[code] || code).join("；");
      reason.textContent = `${reasonText || "等待人工核对触发依据"}。不可执行，需进入候选与模拟复核。`;
      card.append(title, value, reason);
      return card;
    }));
  }

  document.getElementById("autopilot-center-next").textContent = `${view.primary_action.label}：${view.primary_action.detail}`;
  const primary = document.getElementById("autopilot-center-primary");
  primary.dataset.action = view.primary_action.id;
  primary.textContent = view.primary_action.label;
  primary.disabled = false;
  const strategy = policy.buildModeDraft(view.settings, view);
  root.dataset.strategyMode = strategy.mode;
  root.dataset.executionKind = strategy.execution_kind;
  renderAutopilotPackage(currentAutopilotPackagePreview);
  renderAutopilotOperatingLoop(view.business_strategy || {}, view.recovery_policy || {});
  renderAutopilotRealtimeTask(view.realtime_task || {});
  renderAutopilotActivity();
}

function qianchuanAccountEvidence(account = {}) {
  const key = String(account.key || "");
  const tail = key.replace(/[^a-z0-9]/gi, "").slice(-6).toUpperCase() || "LOCAL";
  const sourceKey = String(account.evidence_source || "unknown");
  const sourceLabels = {
    official_api: "官方 API",
    oauth: "官方授权",
    browser: "浏览器页面",
    browser_page: "浏览器页面",
    manual_confirmation: "人工确认",
    unknown: "来源待确认",
  };
  const source = sourceLabels[sourceKey] || `本机识别（${sourceKey.slice(0, 24)}）`;
  const confidenceKey = String(account.confidence || "medium").toLowerCase();
  const confidence = ({ high: "高", medium: "中", low: "低" })[confidenceKey] || "待核对";
  const lastSeenValue = String(account.last_seen || "").trim();
  const parsedTime = Date.parse(lastSeenValue);
  const lastSeen = lastSeenValue
    ? Number.isFinite(parsedTime) ? new Date(parsedTime).toLocaleString() : lastSeenValue.slice(0, 32)
    : "暂无记录";
  return {
    optionLabel: `匿名账户 ${tail} · ${source} · 置信度${confidence}`,
    summary: `已选账户证据：匿名尾号 ${tail}｜来源 ${source}｜置信度 ${confidence}｜最后发现 ${lastSeen}`,
  };
}

function renderSelectedQianchuanAccountEvidence(accounts = currentQianchuanCatalog?.unlinked_accounts || []) {
  const select = document.getElementById("unlinked-account-select");
  const summary = document.getElementById("unlinked-account-evidence");
  if (!select || !summary) return;
  const account = accounts.find((item) => String(item.key || "") === select.value);
  summary.textContent = account
    ? qianchuanAccountEvidence(account).summary
    : "请选择一个账户查看匿名尾号、证据来源、置信度和最后发现时间。";
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

const CHENGFANG_AUTOPILOT_BLOCKERS = {
  CHENGFANG_MODE_UNVERIFIED: "尚未确认当前为乘方模式",
  SCOPE_BINDING_INCOMPLETE: "店铺、千川账户、超级策略或指标版本绑定不完整",
  PAGE_FINGERPRINT_UNVERIFIED: "真实乘方页面指纹尚未验真",
  FIELD_CONTRACT_UNVERIFIED: "乘方字段合同尚未验真",
  READBACK_CONTRACT_UNVERIFIED: "提交后的回读合同尚未验真",
  DATA_STALE_OR_UNTIMED: "数据已过期或没有可信时间",
  DATA_COMPLETENESS_LOW: "数据完整度不足",
  DATA_CONTRACT_CONFLICT: "投放模式或指标口径存在冲突",
  PROMOTION_MODE_UNVERIFIED: "投放模式证据尚未通过验证",
  ACCOUNT_SCOPE_INCOMPLETE: "当前店铺或千川账户身份不完整",
  ACCOUNT_SCOPE_CONFLICT: "当前店铺与千川账户归属发生冲突",
  METRIC_CONTRACT_UNVERIFIED: "综合 ROI 指标口径未确认",
  AUTOPILOT_POLICY_INCOMPLETE: "预算、频率、冷却和亏损边界尚未补齐",
  EMERGENCY_STOP_ACTIVE: "紧急停止保持开启",
  SHADOW_VALIDATION_INCOMPLETE: "7 天影子验证尚未达到准入门槛",
  OFFICIAL_WRITE_CONTRACT_UNVERIFIED: "官方乘方写接口尚未完成账户级验权",
  ACTION_PREFLIGHT_UNAVAILABLE: "尚无同时通过合同、策略与执行前检查的动作",
  EXECUTION_RUNTIME_INCOMPLETE: "幂等、并发锁、审计、回读或回滚能力未齐备",
  BUSINESS_PROFILE_INCOMPLETE: "经营目标、成本口径或经营边界不完整",
  DECISION_EVIDENCE_INCOMPLETE: "当前预算、ROI、消耗、订单或亏损证据不完整",
  MANUAL_DATA_COMPLETENESS_LOW: "手工证据完整度低于 80%",
  MANUAL_DATA_STALE: "手工证据已超过 30 分钟",
  UNIT_ECONOMICS_NOT_READY: "单件经济模型尚未达到可计算条件",
  ROI_SAMPLE_INSUFFICIENT: "低 ROI 样本不足 100 元消耗或 3 笔归因订单",
};

const CHENGFANG_DECISION_REASONS = {
  BUDGET_CAP_EXCEEDED: "当前总预算超过本店上限",
  DAILY_LOSS_CAP_REACHED: "今日亏损达到本店止损线",
  INVENTORY_FLOOR_BREACHED: "库存覆盖低于安全底线",
  REFUND_CEILING_EXCEEDED: "退款率超过预警线",
  CONTRIBUTION_MARGIN_BELOW_FLOOR: "单件贡献毛利低于安全线",
  ROI_BELOW_BREAK_EVEN: "综合 ROI 低于测算保本线",
};

function renderChengfangAgentRuntime(runtime = {}) {
  const statusNode = document.getElementById("chengfang-runtime-status");
  if (!statusNode) return;
  currentChengfangAgentRuntime = runtime && typeof runtime === "object" ? runtime : null;
  if (runtime.a2_pilot && typeof runtime.a2_pilot === "object") currentChengfangA2Pilot = runtime.a2_pilot;
  const running = runtime.decision_automation?.running === true;
  const evaluations = Number(runtime.evaluation_count || 0);
  const validatedDays = Number(runtime.shadow_evidence?.validated_days || 0);
  const latest = runtime.latest_evaluation || null;
  const candidates = Array.isArray(runtime.candidates) ? runtime.candidates : [];
  const candidate = candidates.at(-1);
  statusNode.textContent = running ? "A1 影子评估运行中" : "未开启自动评估";
  document.getElementById("chengfang-runtime-count").textContent = `${evaluations} 次评估 · ${validatedDays}/7 有效日`;
  document.getElementById("chengfang-runtime-latest").textContent = latest?.recommendation
    || (running ? "等待 Agent 完成第一次影子评估；真实投放写入保持关闭。" : "开启后由本地 Agent 每 5 分钟评估一次，真实投放写入保持关闭。 ");
  const candidateNode = document.getElementById("chengfang-runtime-candidate");
  if (candidate) {
    const reasons = (candidate.reasons || []).map((code) => CHENGFANG_DECISION_REASONS[code] || code).join("；");
    candidateNode.className = "";
    candidateNode.textContent = `最新影子候选：总预算 ${Number(candidate.current_value).toFixed(2)} → ${Number(candidate.target_value).toFixed(2)}（${reasons || "等待复核"}）。该候选不可执行。`;
  } else {
    candidateNode.className = "empty-state";
    candidateNode.textContent = latest?.status === "blocked"
      ? `尚未生成候选：${(latest.blockers || []).map((code) => CHENGFANG_AUTOPILOT_BLOCKERS[code] || code).join("；") || "证据不足"}。`
      : "当前没有预算候选。";
  }
  const evaluateButton = document.getElementById("chengfang-evaluate-now");
  const stopButton = document.getElementById("chengfang-emergency-stop");
  const feedback = document.getElementById("chengfang-runtime-feedback");
  feedback.hidden = !latest || Boolean(latest.feedback);
  feedback.dataset.evaluationId = latest?.evaluation_id || "";
  feedback.querySelectorAll("button").forEach((button) => { button.disabled = false; });
  evaluateButton.disabled = !running;
  stopButton.disabled = !running;
  renderChengfangTrialConsole(runtime, currentChengfangA2Pilot);
  if (currentChengfangCandidatePath) renderChengfangCandidatePath();
  else renderAutopilotCenter();
  renderAutomationPolicyMatrix();
}

const CHENGFANG_A2_STOP_REASONS = {
  NOT_CONFIGURED: "尚未配置",
  MASTER_SWITCH_OFF: "主开关未开启",
  USER_REQUESTED_A2_STOP: "用户主动停止",
  USER_REQUESTED_FROM_EXTENSION: "用户主动停止",
  ENVIRONMENT_CHANGED_TO_PRODUCTION_READ_ONLY: "已切换生产只读环境",
  AUTO_SYNC_DISABLED_FROM_EXTENSION: "5 分钟同步已关闭",
  PILOT_EVIDENCE_STALE: "经营证据已过期",
  CANDIDATE_EVIDENCE_STALE: "候选证据已过期",
  DAILY_LOSS_CAP_REACHED: "已触发单日亏损止损线",
  READBACK_TIMEOUT: "模拟回读超时",
  READBACK_VALUE_MISMATCH: "模拟回读值不一致",
};

function chengfangTrialRuntime(runtime = currentChengfangAgentRuntime || {}) {
  if (currentChengfangDemoFixture?.runtime) return currentChengfangDemoFixture.runtime;
  return { ...runtime, a2_pilot: currentChengfangA2Pilot || runtime.a2_pilot || {} };
}

function chengfangTrialView() {
  const policy = globalThis.DianChengfangTrialPolicy;
  return policy.deriveView(chengfangTrialRuntime(), currentChengfangTrialConfig, currentExtensionSettings);
}

async function saveChengfangTrialConfig(nextValue) {
  currentChengfangTrialConfig = globalThis.DianChengfangTrialPolicy.normalizeConfig(nextValue);
  await chrome.storage.local.set({ [CHENGFANG_TRIAL_CONFIG_KEY]: currentChengfangTrialConfig });
  renderChengfangTrialConsole();
  return currentChengfangTrialConfig;
}

function shortOpaqueKey(value) {
  const text = String(value || "");
  return text.length > 26 ? `${text.slice(0, 14)}…${text.slice(-8)}` : text;
}

const CHENGFANG_PRODUCTION_BLOCKER_LABELS = Object.freeze({
  COMMERCIAL_RUNTIME_NOT_INCLUDED: "当前安装包未包含乘方生产适配器",
  STORE_NOT_SELECTED: "请先选择当前经营店铺",
  QIANCHUAN_ACCOUNT_NOT_SELECTED: "请先选择一个千川账户",
  OFFICIAL_API_NOT_CONNECTED: "尚未连接巨量引擎官方授权",
  OAUTH_NOT_CONNECTED: "尚未连接巨量引擎官方授权",
  OCEANENGINE_OAUTH_REQUIRED: "尚未连接巨量引擎官方授权",
  REQUIRED_SCOPE_MISSING: "官方授权缺少乘方投放管理权限",
  ADVERTISER_NOT_AUTHORIZED: "当前千川账户不在本次官方授权范围",
  NO_TRUSTED_OFFICIAL_SNAPSHOT: "还没有可信的官方乘方计划快照",
  NO_ELIGIBLE_PRODUCTION_TARGET: "当前没有可用于生产降预算的乘方计划",
  RECORD_STALE: "官方计划快照已过期，请先重新同步",
  TARGET_SNAPSHOT_CHANGED: "计划数据已变化，请重新选择并预检",
  CURRENT_BUDGET_CHANGED: "当前预算已变化，请重新预检",
  TARGET_NOT_AVAILABLE: "所选计划已不可用，请刷新列表",
  PRODUCTION_TARGET_NOT_SELECTED: "请先选择一个乘方计划",
  PRODUCTION_OPERATION_ACTIVE: "已有生产操作未结束，请先完成或取消",
  TARGET_ALREADY_INFLIGHT: "这个计划已有未结束的生产操作",
  LIVE_EXECUTION_AUDIT_LOCKED: "存在结果未知的生产操作，写入已冻结",
  UNKNOWN_WRITE_REQUIRES_RECONCILIATION: "存在结果未知的生产操作，必须先人工对账",
  MANUAL_RECONCILIATION_REQUIRED: "执行结果需要人工核对",
  RAW_IDENTIFIER_EXPOSED: "计划列表包含不应展示的原始标识，已拒绝加载",
  EXECUTION_RESULT_UNKNOWN: "官方写入结果未知，已冻结后续操作",
  EXECUTION_RESULT_UNKNOWN_RELOAD_REQUIRED: "响应中断，已冻结执行；请刷新本机审计状态",
  AUTHORIZATION_EXPIRED: "本次短时授权已过期，请重新预检",
  AUTHORIZATION_ALREADY_USED: "本次授权已使用，不能重复执行",
  CONFIRMATION_PHRASE_MISMATCH: "确认词不完全一致",
  TARGET_BUDGET_NOT_LOWER: "目标预算必须低于当前预算",
  TARGET_BUDGET_DECREASE_TOO_LARGE: "单次降幅不能超过 20%",
  BUDGET_DECREASE_ONLY: "目标预算必须低于当前预算",
  BUDGET_DECREASE_EXCEEDS_20_PERCENT: "单次降幅不能超过 20%",
  PRODUCTION_WRITE_KILL_SWITCH_ACTIVE: "生产紧急停止已开启，解除前不会执行写入",
  PRODUCTION_WRITE_STATUS_UNAVAILABLE: "生产写入状态暂时无法读取",
  OFFICIAL_PLAN_SNAPSHOT_MISSING: "还没有官方乘方计划快照，请先同步当前账户",
  SNAPSHOT_STALE: "官方计划快照已过期，请重新同步当前账户",
  TRUSTED_OFFICIAL_SNAPSHOT_REQUIRED: "当前快照不是可信官方来源，请重新同步",
  SNAPSHOT_STORE_UNAVAILABLE: "暂时无法读取官方计划快照",
  ACCOUNT_BINDING_MISMATCH: "计划快照与当前千川账户不一致",
  ACCOUNT_BINDING_UNVERIFIED: "当前千川账户身份还未核验",
  PRODUCTION_SCOPE_LEASE_REQUIRED: "请先明确绑定当前店铺与千川账户，再进行真实写入",
  SCOPE_LEASE_REVOKED: "店铺或账户绑定已变化，本次旧授权已失效",
  BINDING_LEASE_REVOKED_BEFORE_WRITE: "店铺或账户绑定已变化，官方写入已在请求前阻止",
  SCOPE_CHANGE_BLOCKED_UNRESOLVED_WRITE: "存在未完成对账的真实操作，暂不能切店或解绑",
  BINDING_REGISTRY_INCONSISTENT: "店铺与千川账户绑定记录不一致，请重新绑定",
  OFFICIAL_CHANNEL_REQUIRED: "生产写入只允许使用官方 API 通道",
  OFFICIAL_ADVERTISER_NOT_AUTHORIZED: "当前广告账户不在官方授权范围",
  OFFICIAL_ACCOUNT_AUTHORIZATION_UNAVAILABLE: "暂时无法核验官方账户授权",
  OFFICIAL_ACCESS_TOKEN_UNAVAILABLE: "官方授权已失效，请重新授权",
  READBACK_MISMATCH_REQUIRES_REVIEW: "官方回读与目标预算不一致，已停止继续写入",
});

const CHENGFANG_PRODUCTION_GOAL_LABELS = Object.freeze({
  PRODUCT: "商品成交", PRODUCT_SALES: "商品成交", LIVE: "直播成交", LIVE_SALES: "直播成交",
  GMV: "成交增长", ROI: "投产控制", PAY_ROI: "支付 ROI", COST: "成本控制",
});

const CHENGFANG_PRODUCTION_SCENE_LABELS = Object.freeze({
  PRODUCT: "商品场景", SHOP: "商城场景", LIVE: "直播场景", VIDEO: "短视频场景",
  ALL: "全域场景", FULL_DOMAIN: "全域场景", SEARCH: "搜索场景",
});

function chengfangProductionObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function chengfangProductionMoney(value, fallback = "待产生") {
  return value !== null && value !== "" && Number.isFinite(Number(value))
    ? `¥${Number(value).toFixed(2)}`
    : fallback;
}

function chengfangProductionNumber(...values) {
  for (const value of values) {
    if (value !== null && value !== "" && Number.isFinite(Number(value))) return Number(value);
  }
  return null;
}

function chengfangProductionBlockerLabel(value) {
  const code = String(value || "").trim().toUpperCase();
  return CHENGFANG_PRODUCTION_BLOCKER_LABELS[code] || "当前账户尚未满足受控生产写入条件";
}

function chengfangProductionReadiness(state = {}, phase = "idle") {
  const value = chengfangProductionObject(state);
  const status = chengfangProductionObject(value.status);
  const rawBlockers = value.unsafe_target_response
    ? ["RAW_IDENTIFIER_EXPOSED"]
    : [...(Array.isArray(status.blockers) ? status.blockers : []), ...(Array.isArray(value.operation?.blockers) ? value.operation.blockers : [])];
  const blockers = Array.from(new Set(rawBlockers.map((item) => String(item || "").trim().toUpperCase()).filter(Boolean)));
  const hasAny = (codes) => codes.some((code) => blockers.includes(code));
  const unknown = phase === "unknown" || hasAny([
    "UNKNOWN_WRITE_REQUIRES_RECONCILIATION", "LIVE_EXECUTION_AUDIT_LOCKED",
    "MANUAL_RECONCILIATION_REQUIRED", "EXECUTION_RESULT_UNKNOWN",
    "EXECUTION_RESULT_UNKNOWN_RELOAD_REQUIRED",
  ]);
  const killSwitch = value.kill_switch?.active === true || blockers.includes("PRODUCTION_WRITE_KILL_SWITCH_ACTIVE");
  const storeReady = !blockers.includes("STORE_NOT_SELECTED");
  const bindingBlockers = [
    "QIANCHUAN_ACCOUNT_NOT_SELECTED", "ACCOUNT_BINDING_MISMATCH", "ACCOUNT_BINDING_UNVERIFIED",
    "PRODUCTION_SCOPE_LEASE_REQUIRED", "SCOPE_LEASE_REVOKED", "BINDING_LEASE_REVOKED_BEFORE_WRITE",
    "BINDING_REGISTRY_INCONSISTENT",
  ];
  const accountReady = storeReady && !hasAny(bindingBlockers);
  const oauthBlockers = [
    "OFFICIAL_API_NOT_CONNECTED", "OAUTH_NOT_CONNECTED", "OCEANENGINE_OAUTH_REQUIRED",
    "REQUIRED_SCOPE_MISSING", "ADVERTISER_NOT_AUTHORIZED", "OFFICIAL_ADVERTISER_NOT_AUTHORIZED",
    "OFFICIAL_ACCOUNT_AUTHORIZATION_UNAVAILABLE", "OFFICIAL_ACCESS_TOKEN_UNAVAILABLE",
  ];
  const oauthReady = accountReady && !hasAny(oauthBlockers);
  const snapshotBlockers = [
    "NO_TRUSTED_OFFICIAL_SNAPSHOT", "OFFICIAL_PLAN_SNAPSHOT_MISSING", "SNAPSHOT_STALE",
    "RECORD_STALE", "TRUSTED_OFFICIAL_SNAPSHOT_REQUIRED", "SNAPSHOT_STORE_UNAVAILABLE",
    "NO_ELIGIBLE_PRODUCTION_TARGET", "TARGET_NOT_AVAILABLE", "TARGET_SNAPSHOT_CHANGED",
    "ACCOUNT_BINDING_MISMATCH", "ACCOUNT_BINDING_UNVERIFIED", "PRODUCTION_SCOPE_LEASE_REQUIRED",
    "SCOPE_LEASE_REVOKED", "BINDING_LEASE_REVOKED_BEFORE_WRITE", "BINDING_REGISTRY_INCONSISTENT",
  ];
  const targetReady = oauthReady && Array.isArray(value.targets) && value.targets.length > 0 && !hasAny(snapshotBlockers);
  const operationActive = ["prepared", "authorized", "executing", "awaiting_readback"].includes(phase);
  const executeReady = targetReady && status.ready === true && !unknown && !killSwitch && !operationActive;

  let action = "refresh";
  if (unknown) action = "reconcile";
  else if (killSwitch) action = "resume";
  else if (blockers.includes("READBACK_MISMATCH_REQUIRES_REVIEW")) action = "stop";
  else if (blockers.includes("COMMERCIAL_RUNTIME_NOT_INCLUDED")) action = "version";
  else if (!storeReady) action = "store";
  else if (!accountReady) action = "account";
  else if (!oauthReady) action = "oauth";
  else if (!targetReady) action = "sync";
  else if (operationActive) action = "operation";
  else if (executeReady) action = "select";

  const actionCopy = {
    reconcile: ["先处理未知结果", "禁止重试；人工核对后只读查询官方结果。", "查询官方结果"],
    resume: ["生产紧停仍在生效", "确认没有未知操作后，输入精确恢复口令。", "解除生产紧停"],
    stop: ["官方回读与目标不一致", "立即保持停止并复核官方计划，不继续写入。", "立即紧急停止"],
    version: ["当前版本不含生产适配器", "安装私有商业版后再检查，不会回退到网页点击。", "查看版本与更新"],
    store: ["第 1 步：确认当前店铺", "只确认这次经营对象，不要求录入账号密码。", "确认当前店铺"],
    account: ["第 2 步：选择千川账户", "一次只选择一个账户，避免跨店和跨账户误操作。", "选择千川账户"],
    oauth: ["第 3 步：完成官方授权", "生产写入只走巨量引擎官方 API，不使用网页点击兜底。", "管理官方授权"],
    sync: ["第 4 步：同步可信计划", "只读同步当前账户，得到新鲜的乘方父计划预算。", "同步当前账户计划"],
    operation: ["继续当前唯一操作", "完成当前预检、授权或官方回读后，才能开始下一次。", "查看当前操作"],
    select: ["准备完成，可以选择计划", "先选择一个计划并填写降幅不超过 20% 的目标预算。", "选择唯一计划"],
    refresh: ["正在核验生产条件", "重新读取本机审计与官方快照状态，不会发起写入。", "重新检查"],
  };
  const copy = actionCopy[action];
  const steps = [
    { label: "当前店铺", ready: storeReady },
    { label: "千川账户", ready: accountReady },
    { label: "官方授权", ready: oauthReady },
    { label: "可信计划", ready: targetReady },
    { label: "受控执行", ready: executeReady },
  ];
  const forcedCurrent = unknown || killSwitch || action === "stop" ? 4 : -1;
  const firstPending = steps.findIndex((step) => !step.ready);
  const currentIndex = forcedCurrent >= 0 ? forcedCurrent : firstPending >= 0 ? firstPending : 4;
  return {
    action,
    title: copy[0],
    detail: copy[1],
    button_label: copy[2],
    blockers,
    current_index: currentIndex,
    steps: steps.map((step, index) => ({
      ...step,
      current: index === currentIndex,
      status: step.ready ? "已完成" : index === currentIndex ? "当前处理" : "等待前一步",
    })),
  };
}

function chengfangProductionSafeTarget(value = {}) {
  const target = chengfangProductionObject(value);
  const rawIdentifierKeys = ["advertiser_id", "ad_id", "task_id", "task_ids", "control_tasks", "shop_id", "store_id", "account_id"];
  if (rawIdentifierKeys.some((key) => Object.hasOwn(target, key))) return { unsafe: true, target: null };
  const targetKey = String(target.target_key || "").trim();
  if (!/^cf_target_v1_[a-f0-9]{32}$/i.test(targetKey)) return { unsafe: false, target: null };
  const localShortCode = String(target.local_short_code || "").trim().toUpperCase();
  if (!/^CF-[A-F0-9]{8}$/.test(localShortCode)) return { unsafe: true, target: null };
  const planName = String(target.plan_name || "")
    .slice(0, 960)
    .normalize("NFKC")
    .replace(/[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2060-\u2069]/g, " ")
    .replace(/[<>]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 120) || "未命名乘方计划";
  const affectedTaskCount = Number.isInteger(Number(target.affected_task_count))
    && Number(target.affected_task_count) >= 1
    && Number(target.affected_task_count) <= 2000
    ? Number(target.affected_task_count)
    : 1;
  return {
    unsafe: false,
    target: {
      target_key: targetKey,
      plan_name: planName,
      local_short_code: localShortCode,
      affected_task_count: affectedTaskCount,
      current_total_budget: chengfangProductionNumber(target.current_total_budget),
      marketing_goal: String(target.marketing_goal || "").trim().toUpperCase(),
      scene: String(target.scene || "").trim().toUpperCase(),
      task_status: String(target.task_status || "").trim().toUpperCase(),
      captured_at_ms: Number(target.captured_at_ms || 0),
      single_plan_bound: target.single_plan_bound === true,
      official_snapshot_verified: target.official_snapshot_verified === true,
    },
  };
}

function chengfangProductionSafeOperation(value = {}) {
  const source = chengfangProductionObject(value);
  const operationId = String(source.operation_id || "").trim();
  if (!operationId) return null;
  const readback = chengfangProductionObject(source.readback);
  const adapterReceipt = chengfangProductionObject(source.adapter_receipt);
  const officialReadback = chengfangProductionObject(source.official_readback);
  const outcomeReason = String(source.outcome_reason || "").trim().toUpperCase();
  let operationState = String(source.state || source.status || source.phase || "").trim().toLowerCase();
  if (operationState === "rejected" && outcomeReason === "HUMAN_CANCELED_BEFORE_WRITE") operationState = "cancelled";
  return {
    operation_id: operationId.slice(0, 200),
    target_key: String(source.target_key || "").trim(),
    state: operationState,
    outcome: String(source.outcome || adapterReceipt.outcome || officialReadback.outcome || "").trim().toLowerCase(),
    outcome_reason: outcomeReason,
    request_outcome: String(source.request_outcome || "").trim().toLowerCase(),
    observed_state: String(source.observed_state || "").trim().toLowerCase(),
    safety_resolution: String(source.safety_resolution || "").trim().toLowerCase(),
    reconciliation_status: String(source.reconciliation_status || "").trim().toLowerCase(),
    confirmation_phrase: String(source.confirmation_phrase || source.required_confirmation_phrase || ""),
    current_budget: chengfangProductionNumber(source.current_budget, source.current_total_budget, source.current_value, source.before_budget, source.before_value),
    target_budget: chengfangProductionNumber(source.target_budget, source.target_value),
    observed_budget: chengfangProductionNumber(
      source.observed_budget, source.readback_budget, source.official_budget, readback.observed_budget, readback.observed_value,
      officialReadback.observed_value, officialReadback.reconcile_observed_value,
      adapterReceipt.observed_value, adapterReceipt.reconcile_observed_value,
    ),
    matched: source.matched === true || readback.matched === true || officialReadback.matched === true || adapterReceipt.matched === true,
    authorized: source.authorized === true || source.authorization_granted === true,
    consumed: source.consumed === true || source.authorization_consumed === true,
    cancelled: source.cancelled === true,
    manual_reconcile: source.manual_reconcile === true || source.manual_reconcile_required === true || source.manual_reconciliation_required === true,
    expires_at_ms: Number(source.expires_at_ms || source.authorization_expires_at_ms || 0),
    blockers: Array.isArray(source.blockers) ? source.blockers.map((item) => String(item || "").toUpperCase()).slice(0, 20) : [],
  };
}

function normalizeChengfangProductionWrite(payload = {}) {
  const value = chengfangProductionObject(payload);
  const productionWrite = chengfangProductionObject(value.production_write);
  const summaryValue = Object.keys(productionWrite).length ? productionWrite : value;
  const statusValue = typeof value.status === "string" ? { state: value.status }
    : Object.hasOwn(value, "status") ? chengfangProductionObject(value.status)
      : typeof summaryValue.status === "string" ? { ...summaryValue, state: summaryValue.status }
        : Object.hasOwn(summaryValue, "status") ? { ...summaryValue, ...chengfangProductionObject(summaryValue.status) } : summaryValue;
  const status = {
    state: String(statusValue.state || statusValue.phase || "").trim().toLowerCase(),
    ready: statusValue.ready === true || statusValue.available === true,
    write_enabled: statusValue.write_enabled === true || statusValue.production_write_enabled === true,
    frozen: statusValue.frozen === true || statusValue.write_frozen === true,
    blockers: Array.isArray(statusValue.blockers)
      ? statusValue.blockers.map((item) => String(item || "").toUpperCase()).slice(0, 20)
      : Array.isArray(summaryValue.blockers) ? summaryValue.blockers.map((item) => String(item || "").toUpperCase()).slice(0, 20) : [],
  };
  const targetSource = Object.hasOwn(value, "targets") ? value.targets : summaryValue.targets;
  const targetContainer = Array.isArray(targetSource) ? targetSource : chengfangProductionObject(targetSource).targets;
  let unsafeTargetResponse = false;
  const targets = (Array.isArray(targetContainer) ? targetContainer : []).flatMap((item) => {
    const safe = chengfangProductionSafeTarget(item);
    if (safe.unsafe) unsafeTargetResponse = true;
    return safe.target ? [safe.target] : [];
  });
  const active = chengfangProductionObject(Object.hasOwn(value, "active") ? value.active : summaryValue.active);
  const operationsSource = Object.hasOwn(value, "operations") ? value.operations : summaryValue.operations;
  const operations = Array.isArray(operationsSource) ? operationsSource : [];
  const recoveredOperation = operations.find((item) => {
    const itemState = String(chengfangProductionObject(item).status || "").toLowerCase();
    return itemState === "unknown" || chengfangProductionObject(item).manual_reconcile_required === true;
  }) || operations.find((item) => ["prepared", "authorized", "executing"].includes(String(chengfangProductionObject(item).status || "").toLowerCase())) || operations[0];
  const directOperation = chengfangProductionSafeOperation(value.operation)
    || chengfangProductionSafeOperation(active.operation)
    || chengfangProductionSafeOperation(active)
    || chengfangProductionSafeOperation(recoveredOperation)
    || chengfangProductionSafeOperation(value);
  const killValue = chengfangProductionObject(
    Object.hasOwn(value, "kill_switch") ? value.kill_switch : summaryValue.kill_switch,
  );
  const killSwitch = {
    active: killValue.active === true,
    reason: String(killValue.reason || "").slice(0, 160),
    resume_confirmation_phrase: String(killValue.resume_confirmation_phrase || "").slice(0, 120),
  };
  return { status, targets, operation: directOperation, kill_switch: killSwitch, unsafe_target_response: unsafeTargetResponse };
}

function applyChengfangProductionWrite(payload = {}, { replace = false } = {}) {
  const value = chengfangProductionObject(payload);
  const normalized = normalizeChengfangProductionWrite(value);
  const productionWrite = chengfangProductionObject(value.production_write);
  const hasTargets = Object.hasOwn(value, "targets") || Object.hasOwn(chengfangProductionObject(value.production_write), "targets");
  const hasOperation = replace || Object.hasOwn(value, "operation") || Object.hasOwn(value, "active")
    || Object.hasOwn(productionWrite, "active") || Object.hasOwn(productionWrite, "operations") || Boolean(normalized.operation);
  const previousOperation = currentChengfangProductionWrite.operation;
  let nextOperation = normalized.operation;
  if (nextOperation && previousOperation?.operation_id === nextOperation.operation_id) {
    nextOperation = Object.fromEntries(Object.entries({ ...previousOperation, ...nextOperation }).map(([key, fieldValue]) => {
      if ((fieldValue === null || fieldValue === "") && previousOperation[key] !== null && previousOperation[key] !== "" && previousOperation[key] !== undefined) {
        return [key, previousOperation[key]];
      }
      return [key, fieldValue];
    }));
  }
  currentChengfangProductionWrite = {
    status: replace ? normalized.status : { ...currentChengfangProductionWrite.status, ...normalized.status },
    targets: hasTargets || replace ? normalized.targets : currentChengfangProductionWrite.targets,
    active: hasOperation ? nextOperation : currentChengfangProductionWrite.active,
    operation: hasOperation ? nextOperation : currentChengfangProductionWrite.operation,
    kill_switch: replace ? normalized.kill_switch : { ...currentChengfangProductionWrite.kill_switch, ...normalized.kill_switch },
    unsafe_target_response: normalized.unsafe_target_response || (!hasTargets && currentChengfangProductionWrite.unsafe_target_response),
  };
  renderChengfangProductionWrite();
  return currentChengfangProductionWrite;
}

function chengfangProductionState(operation = currentChengfangProductionWrite.operation, status = currentChengfangProductionWrite.status) {
  const op = chengfangProductionObject(operation);
  const candidates = [op.state, op.outcome, op.reconciliation_status, status.state].map((item) => String(item || "").toLowerCase());
  if (op.manual_reconcile || status.frozen || candidates.some((item) => ["unknown", "ambiguous", "manual_reconcile", "manual_reconciliation", "manual_reconciliation_required"].includes(item))) return "unknown";
  return candidates.find((item) => ["prepared", "authorized", "executing", "awaiting_readback", "verified", "observed", "succeeded", "completed", "failed", "rejected", "cancelled", "expired"].includes(item)) || "idle";
}

function chengfangProductionTargetLabel(target = {}, index = 0) {
  const goal = CHENGFANG_PRODUCTION_GOAL_LABELS[target.marketing_goal] || "经营目标";
  const scene = CHENGFANG_PRODUCTION_SCENE_LABELS[target.scene] || "乘方场景";
  const planName = String(target.plan_name || "未命名乘方计划");
  const shortCode = String(target.local_short_code || "本地短码待核对");
  const taskCount = Math.max(1, Number(target.affected_task_count || 1));
  return `${planName} · ${shortCode} · ${goal} · ${scene} · 影响 ${taskCount} 个任务 · 当前预算 ${chengfangProductionMoney(target.current_total_budget, "待核对")}`;
}

function chengfangProductionBudgetDraft() {
  const select = document.getElementById("chengfang-production-target");
  const target = currentChengfangProductionWrite.targets.find((item) => item.target_key === select?.value) || null;
  const operation = currentChengfangProductionWrite.operation;
  const operationControlsForm = ["prepared", "authorized", "executing", "awaiting_readback", "unknown"]
    .includes(chengfangProductionState(operation, currentChengfangProductionWrite.status));
  const currentBudget = operationControlsForm && operation?.target_key === target?.target_key
    ? chengfangProductionNumber(operation.current_budget, target?.current_total_budget)
    : chengfangProductionNumber(target?.current_total_budget);
  const targetBudget = chengfangProductionNumber(document.getElementById("chengfang-production-budget")?.value);
  const valid = Boolean(target && currentBudget !== null && targetBudget !== null && targetBudget > 0
    && targetBudget < currentBudget && targetBudget >= currentBudget * 0.8 - 0.005);
  return { target, currentBudget, targetBudget, valid };
}

function renderChengfangProductionWrite() {
  const root = document.getElementById("chengfang-production-write");
  if (!root) return;
  const state = currentChengfangProductionWrite;
  const operation = state.operation;
  const phase = chengfangProductionState(operation, state.status);
  const unknown = phase === "unknown";
  const killSwitchActive = state.kill_switch?.active === true;
  const authorizationExpired = phase === "authorized" && Number(operation?.expires_at_ms || 0) > 0 && Date.now() > Number(operation.expires_at_ms);
  const operationActive = ["prepared", "authorized", "executing", "awaiting_readback", "unknown"].includes(phase);
  const select = document.getElementById("chengfang-production-target");
  const previousSelection = operationActive ? operation?.target_key || select.value : select.value;
  select.replaceChildren();
  if (!state.targets.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = state.unsafe_target_response ? "计划标识校验失败，已拒绝加载" : "等待可信官方快照";
    select.append(option);
  } else {
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "请选择一个计划";
    select.append(placeholder, ...state.targets.map((target, index) => {
      const option = document.createElement("option");
      option.value = target.target_key;
      option.textContent = chengfangProductionTargetLabel(target, index);
      return option;
    }));
  }
  select.value = state.targets.some((target) => target.target_key === previousSelection) ? previousSelection : "";
  select.disabled = operationActive || killSwitchActive;

  const draft = chengfangProductionBudgetDraft();
  const budgetInput = document.getElementById("chengfang-production-budget");
  if (operationActive && operation?.operation_id && budgetInput.dataset.operationId !== operation.operation_id) {
    budgetInput.dataset.operationId = operation.operation_id;
    if (operation.target_budget !== null) budgetInput.value = Number(operation.target_budget).toFixed(2);
    const confirmationInput = document.getElementById("chengfang-production-confirmation");
    confirmationInput.value = "";
    confirmationInput.dataset.requiredPhrase = "";
  } else if (!operationActive || !operation?.operation_id) {
    delete budgetInput.dataset.operationId;
  }
  const refreshedDraft = chengfangProductionBudgetDraft();
  budgetInput.disabled = operationActive || killSwitchActive;
  budgetInput.max = refreshedDraft.currentBudget === null ? "" : Math.max(0, refreshedDraft.currentBudget - 0.01).toFixed(2);
  budgetInput.min = refreshedDraft.currentBudget === null ? "0.01" : Math.max(0.01, refreshedDraft.currentBudget * 0.8).toFixed(2);

  document.getElementById("chengfang-production-target-count").textContent = `${state.targets.length} 个可选`;
  document.getElementById("chengfang-production-current-budget").textContent = chengfangProductionMoney(refreshedDraft.currentBudget, "待读取");
  const change = refreshedDraft.currentBudget !== null && refreshedDraft.targetBudget !== null
    ? refreshedDraft.targetBudget < refreshedDraft.currentBudget
      ? `下降 ${((refreshedDraft.currentBudget - refreshedDraft.targetBudget) / refreshedDraft.currentBudget * 100).toFixed(2)}%`
      : "必须低于当前预算"
    : "等待填写";
  document.getElementById("chengfang-production-change").textContent = change;

  const observed = operation?.observed_budget;
  const observedMatchesTarget = observed !== null && operation?.target_budget !== null
    && Math.abs(Number(observed) - Number(operation.target_budget)) <= 0.005;
  const readbackMismatch = ["verified", "observed", "succeeded", "completed"].includes(phase)
    && !(operation?.matched === true && observedMatchesTarget);
  const blockers = state.unsafe_target_response
    ? ["RAW_IDENTIFIER_EXPOSED"]
    : [...(state.status.blockers || []), ...(operation?.blockers || []), ...(readbackMismatch ? ["READBACK_MISMATCH_REQUIRES_REVIEW"] : [])];
  const firstBlocker = blockers.length ? chengfangProductionBlockerLabel(blockers[0]) : "";
  const productionReady = state.status.ready === true && !blockers.length && !readbackMismatch;
  const readiness = chengfangProductionReadiness({
    ...state,
    status: { ...state.status, blockers },
  }, readbackMismatch ? "unknown" : phase);
  const readinessList = document.getElementById("chengfang-production-readiness-steps");
  readinessList.replaceChildren(...readiness.steps.map((step, index) => {
    const item = document.createElement("li");
    item.className = step.current ? "current" : step.ready ? "ready" : "pending";
    if (step.current) item.setAttribute("aria-current", "step");
    const marker = document.createElement("b");
    marker.textContent = step.ready ? "✓" : String(index + 1);
    const label = document.createElement("span");
    label.textContent = step.label;
    const statusText = document.createElement("small");
    statusText.textContent = step.status;
    item.append(marker, label, statusText);
    return item;
  }));
  document.getElementById("chengfang-production-readiness-title").textContent = readiness.title;
  document.getElementById("chengfang-production-readiness-detail").textContent = readiness.detail;
  const nextButton = document.getElementById("chengfang-production-next");
  nextButton.dataset.action = readiness.action;
  nextButton.textContent = readiness.button_label;
  const banner = document.getElementById("chengfang-production-banner");
  const title = document.getElementById("chengfang-production-state");
  const summary = document.getElementById("chengfang-production-summary");
  if (readbackMismatch) {
    banner.className = "chengfang-trial-banner danger";
    title.textContent = "官方回读与目标预算不一致";
    summary.textContent = "界面已按异常冻结；请立即开启生产紧停并复核官方计划。";
  } else if (unknown) {
    banner.className = "chengfang-trial-banner danger";
    title.textContent = "结果未知 · 生产写入已冻结";
    summary.textContent = "禁止再次执行或取消；请先人工核对，再只读查询官方结果。";
  } else if (killSwitchActive) {
    banner.className = "chengfang-trial-banner danger";
    title.textContent = "生产紧急停止已开启";
    summary.textContent = "预检、授权和执行均由服务端阻断；无未决结果后可用精确口令恢复。";
  } else if (phase === "observed") {
    banner.className = "chengfang-trial-banner ready";
    title.textContent = "目标预算已由官方接口再次观察";
    summary.textContent = "当前预算已经等于目标值，但无法证明由本次请求造成；安全冻结已解除，不会把它记为本请求成功。";
  } else if (["verified", "succeeded", "completed"].includes(phase)) {
    banner.className = "chengfang-trial-banner ready";
    title.textContent = "本次官方降预算已回读验收";
    summary.textContent = "可以刷新可信快照后发起下一次独立操作。";
  } else if (operationActive) {
    banner.className = "chengfang-trial-banner warning";
    title.textContent = authorizationExpired ? "一次性授权已过期" : phase === "authorized" ? "一次性短时授权已就绪" : phase === "prepared" ? "预检通过 · 等待精确确认" : "官方操作处理中";
    summary.textContent = authorizationExpired ? "过期授权不能执行；请取消本次操作后重新预检。" : "本次操作只绑定一个计划和一个目标预算；不会自动重试或点击网页。";
  } else if (state.targets.length && productionReady) {
    banner.className = "chengfang-trial-banner ready";
    title.textContent = "可申请单计划降预算";
    summary.textContent = "选择计划和目标预算后先预检；此时不会写入千川。";
  } else {
    banner.className = "chengfang-trial-banner warning";
    title.textContent = "生产写入尚未就绪";
    summary.textContent = firstBlocker || "请先完成官方授权并同步可信乘方计划。";
  }

  const prepare = document.getElementById("chengfang-production-prepare");
  prepare.disabled = !refreshedDraft.valid || !productionReady || operationActive || unknown || killSwitchActive;
  const authorization = document.getElementById("chengfang-production-authorization");
  authorization.hidden = !operation?.operation_id || !["prepared", "authorized", "executing", "awaiting_readback", "unknown"].includes(phase);
  const phrase = String(operation?.confirmation_phrase || "");
  const confirmationInput = document.getElementById("chengfang-production-confirmation");
  if (confirmationInput.dataset.requiredPhrase !== phrase) {
    confirmationInput.value = "";
    confirmationInput.dataset.requiredPhrase = phrase;
  }
  confirmationInput.disabled = phase !== "prepared" || unknown || killSwitchActive;
  document.getElementById("chengfang-production-required-confirmation").textContent = phrase || "等待预检生成";
  const operationTarget = state.targets.find((item) => item.target_key === operation?.target_key);
  const operationPlanLabel = operationTarget
    ? `${operationTarget.plan_name}（${operationTarget.local_short_code}）`
    : "本次唯一父计划";
  document.getElementById("chengfang-production-proposal").textContent = operation
    ? phase === "prepared" && !phrase
      ? `本次唯一动作：${operationPlanLabel}预算从 ${chengfangProductionMoney(operation.current_budget)} 降至 ${chengfangProductionMoney(operation.target_budget)}。页面刷新后确认词不会恢复，请取消本次操作并重新预检。`
      : `本次唯一动作：${operationPlanLabel}预算从 ${chengfangProductionMoney(operation.current_budget)} 降至 ${chengfangProductionMoney(operation.target_budget)}。授权只对这一次操作有效。`
    : "预检通过后显示本次唯一动作。";
  document.getElementById("chengfang-production-auth-state").textContent = authorizationExpired ? "授权已过期 · 请取消重做" : phase === "authorized" ? "已授权 · 等待执行" : phase === "prepared" && !phrase ? "确认词已销毁 · 请取消重做" : phase === "prepared" ? "等待精确确认" : unknown ? "已冻结" : "处理中";
  const authorize = document.getElementById("chengfang-production-authorize");
  authorize.disabled = phase !== "prepared" || !phrase || confirmationInput.value !== phrase || unknown || killSwitchActive;
  const execute = document.getElementById("chengfang-production-execute");
  execute.disabled = phase !== "authorized" || authorizationExpired || unknown || killSwitchActive;
  const cancel = document.getElementById("chengfang-production-cancel");
  cancel.disabled = !["prepared", "authorized"].includes(phase) || unknown || killSwitchActive;

  document.getElementById("chengfang-production-result-before").textContent = chengfangProductionMoney(operation?.current_budget);
  document.getElementById("chengfang-production-result-target").textContent = chengfangProductionMoney(operation?.target_budget);
  document.getElementById("chengfang-production-result-readback").textContent = chengfangProductionMoney(observed);
  const outcomeLabel = readbackMismatch ? "回读不一致 · 已冻结"
    : unknown ? "未知 · 已冻结"
    : phase === "observed" && operation?.matched === true && observedMatchesTarget ? "目标值已观察 · 请求归因未知"
    : ["verified", "succeeded", "completed"].includes(phase) && operation?.matched === true && observedMatchesTarget ? "官方目标预算已核验"
      : ["failed", "rejected"].includes(phase) ? "未生效 · 已结束" : phase === "cancelled" ? "已取消 · 未执行" : "待官方回读";
  document.getElementById("chengfang-production-result-outcome").textContent = outcomeLabel;
  document.getElementById("chengfang-production-result-state").textContent = outcomeLabel;
  document.getElementById("chengfang-production-result-detail").textContent = readbackMismatch
    ? "后端状态与官方观察值不一致，不能显示为成功。保持停止，复核计划后再处理。"
    : unknown
    ? "官方写入响应或回读存在歧义。禁止重复执行；必须先人工核对，再发起只读官方查询。"
    : phase === "observed"
      ? "官方接口已再次读到目标预算，因此可以解除安全冻结；但平台回执不足，系统不会宣称这次请求执行成功。"
    : ["verified", "succeeded", "completed"].includes(phase)
      ? "官方接口已观察到本次目标预算；本次授权已经消费，不能再次执行。"
      : phase === "cancelled" ? "本次预检或授权已取消，未调用官方写接口。"
        : phase === "rejected" ? "官方平台拒绝本次写入，或官方回读确认预算未变化；本次操作已经结束。"
          : "执行后立即通过官方接口回读目标预算。页面提示成功不作为验收依据。";
  const unknownPanel = document.getElementById("chengfang-production-unknown");
  unknownPanel.hidden = !unknown;
  const reconcile = document.getElementById("chengfang-production-reconcile");
  reconcile.disabled = !unknown || !operation?.operation_id;
  const stop = document.getElementById("chengfang-production-stop");
  const resume = document.getElementById("chengfang-production-resume");
  stop.hidden = killSwitchActive;
  stop.disabled = killSwitchActive;
  resume.hidden = !killSwitchActive;
  resume.disabled = !killSwitchActive || unknown || ["executing", "awaiting_readback"].includes(phase);

  const notice = document.getElementById("chengfang-notice");
  document.getElementById("chengfang-notice-title").textContent = unknown
    ? "乘方生产结果未知，全部后续写入已冻结"
    : "A1 / A2 仍是模拟；单计划降预算可申请受控生产写入";
  document.getElementById("chengfang-notice-detail").textContent = unknown
    ? "不要重试或取消；先人工核对千川，再使用只读官方查询完成对账。"
    : "生产写入只走官方 API，必须先预检、输入精确确认词、单次授权并完成官方回读；加预算、批量修改和网页点击兜底仍关闭。";
  notice.className = `chengfang-notice ${unknown ? "danger" : productionReady ? "warning" : "danger"}`;
  const centerWrite = document.getElementById("autopilot-center-write");
  if (centerWrite) centerWrite.textContent = unknown ? "生产写入冻结" : productionReady ? "单计划降预算可授权" : "等待官方授权";

  [prepare, authorize, execute, cancel, reconcile, stop, resume].forEach(markAgentWriteControl);
}

function setChengfangProductionError(error, fallback, { freeze = false } = {}) {
  const code = String(error?.code || error?.payload?.code || "").trim().toUpperCase();
  const message = code ? chengfangProductionBlockerLabel(code) : fallback;
  currentChengfangProductionWrite.status = {
    ...currentChengfangProductionWrite.status,
    frozen: freeze || currentChengfangProductionWrite.status.frozen,
    state: freeze ? "unknown" : currentChengfangProductionWrite.status.state,
    blockers: [freeze ? "EXECUTION_RESULT_UNKNOWN_RELOAD_REQUIRED" : code || "PRODUCTION_REQUEST_FAILED"],
  };
  if (freeze && currentChengfangProductionWrite.operation) {
    currentChengfangProductionWrite.operation = { ...currentChengfangProductionWrite.operation, state: "unknown", manual_reconcile: true };
  }
  renderChengfangProductionWrite();
  document.getElementById("chengfang-production-summary").textContent = message;
}

async function refreshChengfangProductionWrite() {
  try {
    const result = await bridgeFetch("/chengfang/production-write");
    return applyChengfangProductionWrite(result, { replace: true });
  } catch (error) {
    currentChengfangProductionWrite = { status: { blockers: ["PRODUCTION_STATUS_UNAVAILABLE"] }, targets: [], active: null, operation: null, kill_switch: {}, unsafe_target_response: false };
    renderChengfangProductionWrite();
    document.getElementById("chengfang-production-summary").textContent = "生产写入状态暂时无法读取；不会尝试网页点击或自动提交。";
    return null;
  }
}

function trialMoney(value) {
  return value !== null && value !== "" && Number.isFinite(Number(value)) ? money(value) : "待补齐";
}

function trialEffectMoney(value) {
  if (value === null || value === "" || !Number.isFinite(Number(value))) return "待产生";
  return money(value);
}

function trialSignedMoney(value) {
  if (value === null || value === "" || !Number.isFinite(Number(value))) return "待产生";
  const number = Number(value);
  if (number === 0) return "¥0.00";
  return `${number > 0 ? "+" : "-"}¥${Math.abs(number).toFixed(2)}`;
}

function trialSignedPercent(value) {
  if (value === null || value === "" || !Number.isFinite(Number(value))) return "待产生";
  const number = Number(value);
  if (number === 0) return "0%";
  return `${number > 0 ? "+" : ""}${Number(number.toFixed(2))}%`;
}

function renderChengfangTrialEffect(effect = {}) {
  document.getElementById("chengfang-trial-effect-state").textContent = effect.synthetic === true
    ? `合成演示 · ${effect.state_label || "闭环已生成"}`
    : effect.state_label || "等待试运行数据";
  document.getElementById("chengfang-trial-effect-evidence").textContent = effect.evidence_notice
    || "数值仅来自当前 A2 候选、模拟执行回执与模拟回读；缺失值不会按 0 展示。";
  document.getElementById("chengfang-trial-effect-current").textContent = trialEffectMoney(effect.current_budget);
  document.getElementById("chengfang-trial-effect-target").textContent = trialEffectMoney(effect.simulated_target);
  document.getElementById("chengfang-trial-effect-change").textContent = trialSignedMoney(effect.budget_change);
  document.getElementById("chengfang-trial-effect-change-pct").textContent = trialSignedPercent(effect.budget_change_pct);

  const receiptNode = document.getElementById("chengfang-trial-effect-receipt");
  const receiptDetail = document.getElementById("chengfang-trial-effect-receipt-detail");
  const receipt = effect.receipt || {};
  if (receipt.present) {
    receiptNode.textContent = receipt.receipt_id ? shortOpaqueKey(receipt.receipt_id) : "模拟回执已产生";
    receiptNode.title = receipt.receipt_id || "";
    const kind = receipt.execution_kind || "类型未标记";
    const adapter = receipt.adapter_id || "适配器未标记";
    const receiptState = receipt.ok === true ? "回执成功" : receipt.ok === false ? "回执失败" : "回执状态未提供";
    const writeState = receipt.platform_write_attempted === false
      ? "明确记录：未尝试平台写入"
      : receipt.platform_write_attempted === true
        ? "异常记录：报告曾尝试平台写入"
        : "是否尝试平台写入：回执未提供";
    receiptDetail.textContent = `${receiptState} · ${kind} · ${adapter} · ${writeState}`;
  } else {
    receiptNode.textContent = "待产生";
    receiptNode.title = "";
    receiptDetail.textContent = effect.execution_id
      ? "已有执行记录但尚无适配器回执，不判断执行结果。"
      : "尚无 simulation 回执，不判断执行结果。";
  }

  const readbackNode = document.getElementById("chengfang-trial-effect-readback");
  const readbackDetail = document.getElementById("chengfang-trial-effect-readback-detail");
  const readback = effect.readback || {};
  readbackNode.className = "";
  if (readback.present) {
    readbackNode.textContent = trialEffectMoney(readback.observed_value);
    const expected = readback.expected_value === null || readback.expected_value === undefined
      ? "目标值未提供"
      : `模拟目标 ${trialEffectMoney(readback.expected_value)}`;
    const match = readback.matched === true
      ? "匹配"
      : readback.matched === false
        ? "不匹配"
        : "匹配结论待产生";
    readbackNode.className = readback.matched === true ? "matched" : readback.matched === false ? "mismatch" : "";
    readbackDetail.textContent = `${readback.source || "来源未标记"} · ${expected} · ${match}`;
  } else {
    readbackNode.textContent = "待产生";
    readbackDetail.textContent = "尚无模拟回读，不判断是否匹配。";
  }

  const observations = document.getElementById("chengfang-trial-effect-observations");
  const nodes = Array.isArray(effect.observation_nodes) ? effect.observation_nodes : [];
  observations.replaceChildren(...nodes.map((item) => {
    const node = document.createElement("div");
    const label = document.createElement("b");
    label.textContent = item.label || item.id || "观察节点";
    const detail = document.createElement("span");
    detail.textContent = item.detail || "仅预留，当前无生产效果数据";
    node.append(label, detail);
    return node;
  }));
}

function renderChengfangOneClickDemo() {
  const section = document.getElementById("chengfang-one-click-demo");
  if (!section) return;
  const demoEnvironment = currentChengfangTrialConfig.environment !== "production";
  section.hidden = !demoEnvironment;
  const proof = document.getElementById("chengfang-one-click-demo-proof");
  if (!currentChengfangDemoFixture) {
    proof.textContent = "SYNTHETIC · DEMO_FIXTURE · platform_write_attempted=false";
    document.getElementById("chengfang-one-click-demo-clear").hidden = true;
    return;
  }
  const writeState = currentChengfangDemoFixture.platform_write_attempted === false
    ? "platform_write_attempted=false"
    : "安全标记异常：演示结果已隐藏";
  proof.textContent = `演示已生成 · ${currentChengfangDemoFixture.fixture_id || "DEMO_FIXTURE"} · ${writeState} · 未保存真实 runtime`;
  document.getElementById("chengfang-one-click-demo-clear").hidden = false;
}

function renderChengfangTrialConsole(runtime = currentChengfangAgentRuntime || {}, a2Pilot = currentChengfangA2Pilot) {
  const root = document.querySelector(".chengfang-trial-console");
  const policy = globalThis.DianChengfangTrialPolicy;
  if (!root || !policy) return;
  if (a2Pilot && typeof a2Pilot === "object") currentChengfangA2Pilot = a2Pilot;
  const view = policy.deriveView(chengfangTrialRuntime(runtime), currentChengfangTrialConfig, currentExtensionSettings);
  const environment = document.getElementById("chengfang-trial-environment");
  environment.value = view.config.environment;
  renderChengfangOneClickDemo();
  const banner = document.getElementById("chengfang-trial-environment-banner");
  banner.className = `chengfang-trial-banner ${view.environment_tone}`;
  document.getElementById("chengfang-trial-environment-label").textContent = view.environment_label;
  document.getElementById("chengfang-trial-write-state").textContent = view.platform_write_label;

  const sync = document.getElementById("chengfang-trial-sync");
  sync.className = `chengfang-trial-sync ${view.five_minute_sync_enabled ? "ready" : "warning"}`;
  document.getElementById("chengfang-trial-sync-state").textContent = view.five_minute_sync_enabled
    ? "每 5 分钟同步已明确开启"
    : "每 5 分钟同步未开启";
  document.getElementById("chengfang-trial-sync-detail").textContent = view.five_minute_sync_enabled
    ? "只读取已经打开且已登录的抖店 / 千川页面；不会自动打开页面，也不会触发投放写入。"
    : "A2 会阻止配置或执行，避免反复评估旧数据；必须由你点击后才会开启。";
  document.getElementById("chengfang-trial-sync-toggle").textContent = view.five_minute_sync_enabled
    ? "关闭每 5 分钟同步"
    : "开启每 5 分钟同步";

  const scope = view.scope_fingerprint;
  document.getElementById("chengfang-trial-scope").textContent = view.synthetic_demo
    ? "SYNTHETIC 合成作用域 · 不属于真实账户"
    : scope
    ? `店铺 × 账户 × 策略 ${shortOpaqueKey(scope)}`
    : "等待店铺、账户与超级策略绑定";
  document.getElementById("chengfang-trial-action").textContent = view.allowed_action_label;
  document.getElementById("chengfang-trial-authorization").textContent = view.authorization_scope;
  document.getElementById("chengfang-trial-level").textContent = view.synthetic_demo ? "A2 合成演示" : view.master_enabled ? "A2 模拟" : "A1 影子";

  const planKey = view.plan_key;
  document.getElementById("chengfang-trial-plan-key").textContent = planKey || "等待完成作用域绑定";
  const configuredKey = view.config.plan_whitelist[0] || "";
  const configuredKeyMatches = Boolean(planKey && configuredKey === planKey);
  const activeKey = view.active_whitelist_ready ? planKey : "";
  const selectedKey = activeKey || (configuredKeyMatches ? configuredKey : "");
  const whitelistItems = document.getElementById("chengfang-trial-whitelist-items");
  whitelistItems.replaceChildren();
  whitelistItems.className = `chengfang-trial-whitelist-items${selectedKey ? "" : " empty"}`;
  if (selectedKey) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.textContent = `${shortOpaqueKey(selectedKey)}${view.master_enabled ? " · 已生效" : " · 点击移除"}`;
    chip.title = selectedKey;
    chip.disabled = view.master_enabled;
    chip.addEventListener("click", () => saveChengfangTrialConfig({ ...view.config, plan_whitelist: [] }));
    whitelistItems.append(chip);
  } else {
    whitelistItems.textContent = "尚未加入当前策略白名单";
  }
  document.getElementById("chengfang-trial-whitelist-count").textContent = `${selectedKey ? 1 : 0} / 1`;
  const whitelistAdd = document.getElementById("chengfang-trial-whitelist-add");
  whitelistAdd.disabled = !planKey || configuredKeyMatches || view.master_enabled;
  whitelistAdd.textContent = configuredKeyMatches || view.active_whitelist_ready ? "当前策略已加入" : "加入当前策略白名单";

  const capInput = document.getElementById("chengfang-trial-pilot-budget-cap");
  if (document.activeElement !== capInput) capInput.value = view.budget_cap ?? "";
  capInput.disabled = view.master_enabled;
  capInput.classList.toggle("invalid", view.budget_cap !== null && (
    view.budget_cap <= 0
    || (view.boundaries.daily_budget_cap !== null && view.budget_cap > view.boundaries.daily_budget_cap)
  ));
  document.getElementById("chengfang-trial-budget-cap").textContent = trialMoney(view.boundaries.daily_budget_cap);
  document.getElementById("chengfang-trial-loss-cap").textContent = trialMoney(view.boundaries.max_daily_loss);
  document.getElementById("chengfang-trial-adjustment-cap").textContent = view.boundaries.max_single_adjustment_pct === null || view.boundaries.max_daily_adjustment_pct === null
    ? "待补齐"
    : `${view.boundaries.max_single_adjustment_pct}% / ${view.boundaries.max_daily_adjustment_pct}%`;
  document.getElementById("chengfang-trial-frequency-cap").textContent = view.boundaries.max_daily_actions === null || view.boundaries.cooldown_minutes === null
    ? "待补齐"
    : `${view.boundaries.max_daily_actions} 次 / 冷却 ${view.boundaries.cooldown_minutes} 分钟`;
  renderChengfangTrialEffect(view.effect);

  const steps = document.getElementById("chengfang-trial-steps");
  steps.replaceChildren(...view.steps.map((item, index) => {
    const card = document.createElement("div");
    card.className = `chengfang-trial-step ${item.state}`;
    const strong = document.createElement("strong");
    strong.textContent = `${index + 1}. ${item.label}`;
    const detail = document.createElement("span");
    detail.textContent = item.detail;
    card.append(strong, detail);
    return card;
  }));
  document.getElementById("chengfang-trial-workflow-state").textContent = view.workflow_state;
  const stopReason = !view.master_enabled && view.stop_reason && !["NOT_CONFIGURED", "MASTER_SWITCH_OFF"].includes(view.stop_reason)
    ? `；上次停止：${CHENGFANG_A2_STOP_REASONS[view.stop_reason] || view.stop_reason}`
    : "";
  const blockers = !view.master_enabled && view.blockers.length ? `；当前还差：${view.blockers.join("；")}` : "";
  document.getElementById("chengfang-trial-next").textContent = `${view.next_step}${stopReason}${blockers}`;

  const start = document.getElementById("chengfang-trial-start");
  start.textContent = view.start_label;
  start.disabled = view.master_enabled || !view.can_configure;
  start.title = view.blockers.join("；");
  document.getElementById("chengfang-trial-review-accept").disabled = !view.can_accept;
  document.getElementById("chengfang-trial-review-reject").disabled = !view.can_reject;
  document.getElementById("chengfang-trial-execute").disabled = !view.can_execute;
  document.getElementById("chengfang-trial-readback").disabled = !view.can_readback;
  document.getElementById("chengfang-trial-stop").disabled = !view.can_stop;
}

function setChengfangTrialError(error, fallback) {
  document.getElementById("chengfang-trial-next").textContent = error?.message || fallback;
}

function applyChengfangA2Response(result = {}) {
  if (result.a2_pilot && typeof result.a2_pilot === "object") currentChengfangA2Pilot = result.a2_pilot;
  renderChengfangTrialConsole();
  return chengfangTrialView();
}

function renderChengfangAutopilot(report = {}) {
  const autopilot = report.autopilot || {};
  renderChengfangAgentRuntime(report.autopilot_runtime || currentChengfangAgentRuntime || {});
  const levels = Array.isArray(autopilot.levels) && autopilot.levels.length
    ? autopilot.levels
    : [
        { level: "L0", label: "只读诊断", ready: true, blockers: [] },
        { level: "L1", label: "自动导航", ready: false, blockers: ["PAGE_FINGERPRINT_UNVERIFIED"] },
        { level: "L2", label: "辅助填写", ready: false, blockers: ["FIELD_CONTRACT_UNVERIFIED"] },
        { level: "L3", label: "受控执行", ready: false, blockers: ["OFFICIAL_WRITE_CONTRACT_UNVERIFIED"] },
      ];
  const grid = document.getElementById("chengfang-permission-grid");
  grid.replaceChildren(...levels.map((item) => {
    const card = document.createElement("article");
    card.className = item.ready ? "active" : "blocked";
    const title = document.createElement("b"); title.textContent = `${item.level} · ${item.label}`;
    const detail = document.createElement("span");
    const blockers = Array.isArray(item.blockers) ? item.blockers : [];
    detail.textContent = item.ready
      ? item.level === "L0" ? "已启用：同步、测算、建议、影子观察" : "本级验收项已满足"
      : CHENGFANG_AUTOPILOT_BLOCKERS[blockers[0]] || blockers[0] || "尚未达到本级验收条件";
    card.append(title, detail);
    return card;
  }));
  const currentLevel = autopilot.current_level || "L0";
  document.getElementById("chengfang-autopilot-summary").textContent = `重要提醒：${currentLevel} 已启用；乘方写入只允许经过验证的官方 API，网页点击不会作为兜底。`;
  const nextGate = autopilot.next_gate || levels.find((item) => !item.ready);
  const nextBlockers = Array.isArray(nextGate?.blockers) ? nextGate.blockers : [];
  document.getElementById("chengfang-autopilot-next-gate").textContent = nextGate ? `${nextGate.level} · ${nextGate.label}` : "全部验收完成";
  const discovered = Array.isArray(autopilot.official_api?.discovered_capabilities)
    ? autopilot.official_api.discovered_capabilities.length
    : Array.isArray(report.field_contract?.official_capability_discovery) ? report.field_contract.official_capability_discovery.length : 0;
  document.getElementById("chengfang-autopilot-next-detail").textContent = nextGate
    ? `${CHENGFANG_AUTOPILOT_BLOCKERS[nextBlockers[0]] || nextBlockers[0] || "等待验收"}；已发现 ${discovered} 项官方文档能力，但公开文档不等于当前账户已获写权限。`
    : "仍需在每次执行前通过短时授权、幂等、并发和快照复核。";
}

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
  const metricReady = Boolean(metric.definition && metric.definition !== "unknown" && metric.version);
  const dataReady = Boolean(quality.deterministic_advice_allowed);
  currentChengfangGate = {
    identity_ready: Boolean(scope.complete && !scope.conflict),
    identity_conflict: Boolean(scope.conflict),
    metric_ready: metricReady,
    data_ready: dataReady,
    next_step: report.next_step || dashboard.next_step || "同步真实乘方页面并完成字段验真。",
    autopilot_level: report.autopilot?.current_level || "L0",
  };
  renderChengfangAutopilot(report);
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
  document.getElementById("chengfang-status-summary").textContent = currentChengfangGate.identity_ready && metricReady && dataReady ? "数据可用于只读诊断" : "仍有数据缺口";
  document.getElementById("chengfang-binding-guide").textContent = scope.conflict
    ? "检测到店铺或千川账户冲突，所有写能力均已阻止。"
    : scope.complete
      ? "店铺与千川账户作用域已确认；当前仍只允许 L0 只读诊断。"
      : "请先打开正确的抖店与千川账户并同步；身份不完整时保持只读。";
  const blockerList = document.getElementById("chengfang-blockers-list");
  blockerList.replaceChildren(...(uniqueBlockers.length ? uniqueBlockers : ["只读数据已就绪；乘方写操作仍保持关闭。"])
    .map((message) => { const li = document.createElement("li"); li.textContent = message; return li; }));
  document.getElementById("chengfang-next-step").textContent = report.next_step || dashboard.next_step || "同步真实乘方页面并完成字段验真。";
  renderChengfangPlanner();
  renderAutopilotCenter();
  renderAutomationPolicyMatrix();
}

function renderChengfangUnavailable(message) {
  renderChengfangReadiness({
    summary: { promotion_mode: "unknown", mode_confidence: "unknown" },
    blockers: [message || "乘方准备度暂时无法读取。"],
    next_step: "确认本地 Agent 已启动，然后重新同步当前千川页面。",
  });
}

const CHENGFANG_LOCAL_PLAN_KEY = "chengfangLocalPlanningV1";
const CHENGFANG_LOCAL_PLAN_SCHEMA_VERSION = 5;

function normalizeChengfangScopePart(value) {
  const normalized = String(value || "").trim().toLowerCase();
  return /^[a-z0-9_-]{1,160}$/.test(normalized) ? normalized : "";
}

function chengfangPlanScope(storeKey = "", qianchuanAccountId = "") {
  const store_key = normalizeChengfangScopePart(storeKey);
  const qianchuan_account_id = normalizeChengfangScopePart(qianchuanAccountId);
  const scope_key = store_key && qianchuan_account_id ? `${store_key}|${qianchuan_account_id}` : "";
  return { store_key, qianchuan_account_id, scope_key };
}

function emptyChengfangLocalPlan(scopeValue = {}) {
  const scope = chengfangPlanScope(scopeValue.store_key, scopeValue.qianchuan_account_id);
  return {
    schema_version: CHENGFANG_LOCAL_PLAN_SCHEMA_VERSION,
    goal: "",
    inputs: {},
    boundaries: {},
    evidence: {},
    shadow: { enabled: false },
    scope: { store_key: scope.store_key, qianchuan_account_id: scope.qianchuan_account_id },
    scope_key: scope.scope_key,
    local_only: true,
    execution_allowed: false,
  };
}

function normalizeChengfangLocalPlanStore(saved = {}) {
  const source = saved?.schema_version === CHENGFANG_LOCAL_PLAN_SCHEMA_VERSION
    && saved.plans_by_scope && typeof saved.plans_by_scope === "object" && !Array.isArray(saved.plans_by_scope)
    ? saved.plans_by_scope
    : {};
  const plans = {};
  Object.entries(source).forEach(([storedKey, plan]) => {
    if (!plan || typeof plan !== "object" || Array.isArray(plan)) return;
    const scope = chengfangPlanScope(plan.scope?.store_key, plan.scope?.qianchuan_account_id);
    if (!scope.scope_key || scope.scope_key !== storedKey) return;
    plans[scope.scope_key] = {
      ...emptyChengfangLocalPlan(scope),
      ...plan,
      schema_version: CHENGFANG_LOCAL_PLAN_SCHEMA_VERSION,
      scope: { store_key: scope.store_key, qianchuan_account_id: scope.qianchuan_account_id },
      scope_key: scope.scope_key,
      local_only: true,
      execution_allowed: false,
    };
  });
  return { schema_version: CHENGFANG_LOCAL_PLAN_SCHEMA_VERSION, plans_by_scope: plans };
}

function chengfangPlanForScope(storeValue = {}, scopeValue = {}) {
  const store = normalizeChengfangLocalPlanStore(storeValue);
  const scope = chengfangPlanScope(scopeValue.store_key, scopeValue.qianchuan_account_id);
  return scope.scope_key && store.plans_by_scope[scope.scope_key]
    ? { ...store.plans_by_scope[scope.scope_key] }
    : null;
}

function upsertChengfangPlanForScope(storeValue = {}, planValue = {}) {
  const store = normalizeChengfangLocalPlanStore(storeValue);
  const scope = chengfangPlanScope(planValue.scope?.store_key, planValue.scope?.qianchuan_account_id);
  if (!scope.scope_key || planValue.scope_key !== scope.scope_key) return store;
  return {
    schema_version: CHENGFANG_LOCAL_PLAN_SCHEMA_VERSION,
    plans_by_scope: {
      ...store.plans_by_scope,
      [scope.scope_key]: {
        ...emptyChengfangLocalPlan(scope),
        ...planValue,
        schema_version: CHENGFANG_LOCAL_PLAN_SCHEMA_VERSION,
        scope: { store_key: scope.store_key, qianchuan_account_id: scope.qianchuan_account_id },
        scope_key: scope.scope_key,
        local_only: true,
        execution_allowed: false,
      },
    },
  };
}

function removeChengfangPlanForScope(storeValue = {}, scopeValue = {}) {
  const store = normalizeChengfangLocalPlanStore(storeValue);
  const scope = chengfangPlanScope(scopeValue.store_key, scopeValue.qianchuan_account_id);
  if (!scope.scope_key) return store;
  const plans = { ...store.plans_by_scope };
  delete plans[scope.scope_key];
  return { schema_version: CHENGFANG_LOCAL_PLAN_SCHEMA_VERSION, plans_by_scope: plans };
}

function chengfangScopeError(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

function currentChengfangProfileScope(expectedScope = null) {
  const current = chengfangPlanScope(selectedStoreKey, selectedQianchuanAccount);
  if (!current.scope_key) {
    throw chengfangScopeError("CHENGFANG_SCOPE_REQUIRED", "请先确认当前店铺与一个千川账户，再保存或评估乘方建档。");
  }
  if (expectedScope?.scope_key && expectedScope.scope_key !== current.scope_key) {
    throw chengfangScopeError("CHENGFANG_SCOPE_CHANGED", "店铺或千川账户已切换，旧建档请求已取消。");
  }
  if (currentChengfangLocalPlan.scope_key !== current.scope_key) {
    throw chengfangScopeError("CHENGFANG_PROFILE_SCOPE_MISMATCH", "当前表单不属于已选择的店铺与千川账户，未发送。");
  }
  const scopeFingerprint = String(currentChengfangAgentRuntime?.scope_fingerprint || "").trim();
  if (expectedScope?.scope_fingerprint && expectedScope.scope_fingerprint !== scopeFingerprint) {
    throw chengfangScopeError("CHENGFANG_SCOPE_CHANGED", "乘方运行作用域已变化，旧建档请求已取消。");
  }
  return { ...current, scope_fingerprint: scopeFingerprint };
}

function chengfangScopedPostPayload(payload = {}, expectedScope = null) {
  const scope = currentChengfangProfileScope(expectedScope);
  return {
    scope,
    body: {
      ...payload,
      expected_scope: {
        store_key: scope.store_key,
        qianchuan_account_id: scope.qianchuan_account_id,
        scope_fingerprint: scope.scope_fingerprint,
        platform_write_enabled: false,
        automatic_submit: false,
      },
    },
  };
}

function validateChengfangScopedResponse(expectedScope = {}, result = {}) {
  currentChengfangProfileScope(expectedScope);
  const responseFingerprint = String(result?.autopilot_runtime?.scope_fingerprint || "").trim();
  if (expectedScope.scope_fingerprint && responseFingerprint !== expectedScope.scope_fingerprint) {
    throw chengfangScopeError("CHENGFANG_SCOPE_RESPONSE_MISMATCH", "本机 Agent 返回了其他乘方作用域，结果已拒绝显示。");
  }
  if (result?.platform_write_attempted === true || result?.autopilot_runtime?.decision_automation?.platform_write_enabled === true) {
    throw chengfangScopeError("CHENGFANG_PRODUCTION_WRITE_BLOCKED", "检测到不允许的生产写入标记，结果已拒绝。");
  }
  return result;
}

const CHENGFANG_BOUNDARY_LABELS = Object.freeze({
  minimum_contribution_margin: "最低单件贡献毛利",
  daily_budget_cap: "单日预算上限",
  daily_loss_cap: "单日最大可承受亏损",
  refund_rate_ceiling: "退款率预警线",
  inventory_days_floor: "库存覆盖底线",
  single_adjustment_cap: "单次预算调整上限",
  daily_adjustment_cap: "单日累计调整上限",
  daily_action_cap: "单日动作次数上限",
  cooldown_minutes: "动作冷却时间",
  authorization_ttl_seconds: "单次授权有效期",
});
const CHENGFANG_EVIDENCE_LABELS = Object.freeze({
  data_completeness: "数据完整度",
  data_freshness_minutes: "数据距今",
  current_total_budget: "当前乘方总预算",
  actual_roi: "当前综合 ROI",
  today_spend: "今日累计消耗",
  attributed_orders: "今日归因订单数",
  natural_flow_ratio: "自然流成交占比",
  paid_order_ratio: "付费归因订单占比",
});

function setChengfangAssistStatus(id, message, tone = "") {
  const node = document.getElementById(id);
  if (!node) return;
  node.className = `chengfang-assist-status ${tone}`.trim();
  node.textContent = message;
}

async function applyChengfangBoundaryDraft() {
  const policy = globalThis.DianChengfangProfileDraft;
  if (!policy) throw new Error("保守草案模块未加载，请重新加载扩展");
  const draft = policy.buildBoundaryDraft(readChengfangBoundaryInputs());
  if (!draft.drafted_fields.length) {
    setChengfangAssistStatus("chengfang-boundary-draft-status", "技术护栏已经有值，未覆盖你的设置。", "safe");
    return;
  }
  const preview = draft.drafted_fields.map((field) => `${CHENGFANG_BOUNDARY_LABELS[field]}：${draft.boundaries[field]}`).join("\n");
  if (!confirm(`将补齐以下保守技术护栏：\n\n${preview}\n\n这只是本机建议，不会写入千川；预算、利润、退款和库存红线仍需你填写。确认使用？`)) {
    setChengfangAssistStatus("chengfang-boundary-draft-status", "已取消，现有经营边界没有变化。", "warning");
    return;
  }
  draft.drafted_fields.forEach((field) => {
    const input = document.querySelector(`[data-chengfang-boundary="${field}"]`);
    if (input) input.value = String(draft.boundaries[field]);
  });
  await saveChengfangLocalPlan();
  const remaining = draft.still_required.length
    ? `还需你确认 ${draft.still_required.map((field) => CHENGFANG_BOUNDARY_LABELS[field]).join("、")}。`
    : "全部边界已齐，请复核后继续。";
  setChengfangAssistStatus("chengfang-boundary-draft-status", `已填入 ${draft.drafted_fields.length} 项保守技术护栏；未覆盖已有值。${remaining}`, draft.still_required.length ? "warning" : "safe");
}

async function hydrateChengfangEvidence(button) {
  const policy = globalThis.DianChengfangProfileDraft;
  if (!policy) throw new Error("证据补齐模块未加载，请重新加载扩展");
  setChengfangAssistStatus("chengfang-evidence-hydrate-status", "正在核对当前店铺、千川账户和最新只读快照…");
  const previewPayload = await bridgeFetch("/chengfang/evidence");
  const preview = policy.evidenceFromHydration(previewPayload);
  if (!preview.fresh || (!preview.hydrated_fields.length && !preview.cleared_fields.length)) {
    const scopeHint = preview.scope_rejections.length ? `（${preview.scope_rejections.join("、")}）` : "";
    const message = preview.status === "stale"
      ? "最新快照已超过 30 分钟，请先同步当前千川页面再重试。"
      : `当前范围没有可补齐的投放证据${scopeHint}；请选择千川账户后同步。`;
    setChengfangAssistStatus("chengfang-evidence-hydrate-status", message, "danger");
    return;
  }
  const changedFields = [...new Set([...preview.hydrated_fields, ...preview.cleared_fields])];
  const overwritten = changedFields.filter((field) => {
    const input = document.querySelector(`[data-chengfang-evidence="${field}"]`);
    return input && input.value !== "";
  });
  if (overwritten.length && !confirm(`最新同步数据将更新：${overwritten.map((field) => CHENGFANG_EVIDENCE_LABELS[field]).join("、")}。\n\n不会改动利润、库存、履约或已实现亏损。确认继续？`)) {
    setChengfangAssistStatus("chengfang-evidence-hydrate-status", "已取消，现有证据没有变化。", "warning");
    return;
  }
  button.disabled = true;
  try {
    const savedPayload = await bridgeFetch("/chengfang/evidence/hydrate", { method: "POST", body: JSON.stringify({}) });
    const saved = policy.evidenceFromHydration(savedPayload);
    if (!saved.fresh || (!saved.hydrated_fields.length && !saved.cleared_fields.length)) throw new Error("快照在保存前已失效，请重新同步后再试");
    saved.hydrated_fields.forEach((field) => {
      const input = document.querySelector(`[data-chengfang-evidence="${field}"]`);
      if (input) input.value = String(saved.values[field]);
    });
    saved.cleared_fields.forEach((field) => {
      const input = document.querySelector(`[data-chengfang-evidence="${field}"]`);
      if (input) input.value = "";
    });
    if (savedPayload.autopilot_runtime) renderChengfangAgentRuntime(savedPayload.autopilot_runtime);
    await saveChengfangLocalPlan();
    const source = saved.production_identifier_verified ? "官方精确计划记录" : "当前账户只读快照";
    const missing = saved.missing_decision_fields.length
      ? `；仍缺 ${saved.missing_decision_fields.map((field) => CHENGFANG_EVIDENCE_LABELS[field] || field).join("、")}`
      : "";
    const cleared = saved.cleared_fields.length ? `，并清除 ${saved.cleared_fields.length} 项官方已确认不可用的旧值` : "";
    setChengfangAssistStatus("chengfang-evidence-hydrate-status", `已从${source}补齐 ${saved.hydrated_fields.length} 项证据${cleared}${missing}。未补齐的经营数据继续由你确认。`, saved.missing_decision_fields.length ? "warning" : "safe");
  } finally {
    button.disabled = false;
  }
}

function readChengfangCalculatorInputs() {
  return Object.fromEntries([...document.querySelectorAll("[data-chengfang-input]")]
    .map((input) => [input.dataset.chengfangInput, input.value]));
}

function readChengfangEvidenceInputs() {
  return Object.fromEntries([...document.querySelectorAll("[data-chengfang-evidence]")]
    .map((input) => [input.dataset.chengfangEvidence, input.value]));
}

function readChengfangBoundaryInputs() {
  return Object.fromEntries([...document.querySelectorAll("[data-chengfang-boundary]")]
    .map((input) => [input.dataset.chengfangBoundary, input.value]));
}

function buildChengfangAgentProfile(expectedScope = null) {
  currentChengfangProfileScope(expectedScope);
  return {
    goal: document.querySelector('input[name="chengfang-goal"]:checked')?.value || "",
    inputs: readChengfangCalculatorInputs(),
    evidence: readChengfangEvidenceInputs(),
    boundaries: readChengfangBoundaryInputs(),
  };
}

function candidatePathRuntimeCandidate() {
  const a2 = currentChengfangA2Pilot || currentChengfangAgentRuntime?.a2_pilot || {};
  const candidates = Array.isArray(a2.candidates) ? a2.candidates : [];
  return candidates.length ? candidates[candidates.length - 1] : null;
}

function buildChengfangCandidatePathInput(calculation, boundaries) {
  const a2 = currentChengfangA2Pilot || currentChengfangAgentRuntime?.a2_pilot || {};
  return {
    identityReady: currentChengfangGate.identity_ready === true,
    identityConflict: currentChengfangGate.identity_conflict === true,
    dataReady: currentChengfangGate.data_ready === true,
    planKey: a2.plan_key || "",
    goal: document.querySelector('input[name="chengfang-goal"]:checked')?.value || "",
    calculation,
    boundaries,
    evidence: readChengfangEvidenceInputs(),
    shadowRunning: currentChengfangAgentRuntime?.decision_automation?.running === true,
    candidate: candidatePathRuntimeCandidate(),
  };
}

function candidatePathFieldSelector(field) {
  if (!field) return "";
  if (field === "goal") return 'input[name="chengfang-goal"]';
  if (globalThis.DianChengfangPlanner.REQUIRED_FIELDS.includes(field)) return `[data-chengfang-input="${field}"]`;
  if (globalThis.DianChengfangPlanner.BOUNDARY_FIELDS.includes(field)) return `[data-chengfang-boundary="${field}"]`;
  if (globalThis.DianChengfangTrialPolicy.EVIDENCE_FIELDS.includes(field)) return `[data-chengfang-evidence="${field}"]`;
  return "";
}

function revealChengfangCandidatePathTarget(action = currentChengfangCandidatePath?.next_action || {}) {
  document.querySelectorAll(".candidate-next-field").forEach((item) => item.classList.remove("candidate-next-field"));
  if (["bind_scope", "resolve_binding"].includes(action.id)) {
    navigateToWorkspaceElement("promotion-plan-center", {
      role: "直播投放",
      workspaceKey: "promotion-overview",
      focusElement: document.getElementById("promotion-plan-account-filter"),
    });
    return;
  }
  if (action.id === "sync_chengfang") {
    const sync = document.getElementById("chengfang-sync");
    navigateToWorkspaceElement(sync, {
      role: "直播投放", workspaceKey: "autopilot-strategy", focusTarget: sync.parentElement,
      focusElement: sync, block: "center",
    });
    return;
  }
  if (action.id === "shadow") {
    navigateToWorkspaceElement("chengfang-shadow-program", {
      role: "直播投放", workspaceKey: "autopilot-strategy",
      focusElement: document.getElementById("chengfang-shadow-toggle"), block: "center",
    });
    return;
  }
  if (action.id === "candidate") {
    navigateToWorkspaceElement("chengfang-trial-console", { role: "直播投放", workspaceKey: "autopilot-strategy" });
    return;
  }
  const selector = candidatePathFieldSelector(action.field);
  const target = selector ? document.querySelector(selector) : null;
  if (target) {
    target.classList.add("candidate-next-field");
    navigateToWorkspaceElement("chengfang-profile", {
      role: "直播投放", workspaceKey: "autopilot-strategy", focusElement: target,
      scrollTarget: target, block: "center",
    });
  }
}

function renderChengfangCandidatePath(calculation, boundaries) {
  const policy = globalThis.DianChengfangTrialPolicy;
  const planner = globalThis.DianChengfangPlanner;
  if (!policy || !planner || !document.getElementById("chengfang-candidate-path")) return;
  const currentCalculation = calculation || planner.calculate(readChengfangCalculatorInputs());
  const currentBoundaries = boundaries || planner.validateBoundaries(readChengfangBoundaryInputs());
  const path = policy.deriveCandidatePath(buildChengfangCandidatePathInput(currentCalculation, currentBoundaries));
  currentChengfangCandidatePath = path;
  document.getElementById("chengfang-candidate-path-progress").textContent = `${path.completed_count} / ${path.total_count} 完成`;
  document.getElementById("chengfang-candidate-path-steps").replaceChildren(...path.steps.map((item, index) => {
    const step = document.createElement("div");
    step.className = `chengfang-candidate-path-step ${item.state}`;
    const label = document.createElement("strong");
    label.textContent = `${item.state === "complete" ? "✓" : index + 1}. ${item.label}`;
    const detail = document.createElement("span");
    detail.textContent = item.detail;
    step.append(label, detail);
    return step;
  }));
  document.getElementById("chengfang-candidate-path-next").textContent = `${path.next_action.label}：${path.next_action.detail}`;
  const action = document.getElementById("chengfang-candidate-path-action");
  action.dataset.action = path.next_action.id;
  action.textContent = path.next_action.label;
  action.disabled = false;
  document.getElementById("chengfang-cost-progress").textContent = `${path.cost.completed_count} / ${path.cost.total_count}`;
  document.getElementById("chengfang-boundary-progress").textContent = `${path.boundaries.completed_count} / ${path.boundaries.total_count}`;
  document.getElementById("chengfang-evidence-progress").textContent = `${path.evidence.completed_count} / ${path.evidence.total_count}`;
  const evidenceStatus = document.getElementById("chengfang-evidence-status");
  evidenceStatus.className = `chengfang-evidence-status ${path.evidence.ready ? "safe" : path.evidence.invalid.length ? "danger" : ""}`;
  evidenceStatus.textContent = path.evidence.ready
    ? "当前投放证据已补齐，完整度与数据时间达到 A1 评估门槛。"
    : path.evidence.quality_blockers.length
      ? path.evidence.quality_blockers.join("；")
      : path.evidence.invalid.length
        ? `${path.evidence.invalid.length} 项证据格式无效，请修正红色字段。`
        : `还缺 ${path.evidence.missing.length} 项当前证据；完整度需达到 80%，数据距今不超过 30 分钟。`;
  document.querySelectorAll("[data-chengfang-evidence]").forEach((input) => {
    input.classList.toggle("invalid", path.evidence.invalid.includes(input.dataset.chengfangEvidence));
  });
  renderAutopilotCenter();
}

async function syncChengfangProfileToAgent(expectedScope = null) {
  const saveStatus = document.getElementById("chengfang-profile-save-status");
  if (saveStatus) {
    saveStatus.className = "";
    saveStatus.textContent = "正在保存到本机 Agent…";
  }
  try {
    const scope = currentChengfangProfileScope(expectedScope);
    const request = chengfangScopedPostPayload({ profile: buildChengfangAgentProfile(scope) }, scope);
    const result = await bridgeFetch("/chengfang/autopilot/profile", {
      method: "POST",
      body: JSON.stringify(request.body),
    });
    validateChengfangScopedResponse(scope, result);
    renderChengfangAgentRuntime(result.autopilot_runtime || {});
    if (saveStatus) {
      saveStatus.className = "safe";
      saveStatus.textContent = "已保存到本机 Agent";
    }
    return result;
  } catch (error) {
    if (saveStatus) {
      saveStatus.className = "danger";
      saveStatus.textContent = error?.message || "保存失败，请检查本地 Agent";
    }
    throw error;
  }
}

function scheduleChengfangProfileSync() {
  clearTimeout(chengfangProfileSyncTimer);
  let scope;
  try {
    scope = currentChengfangProfileScope();
  } catch (_) {
    chengfangProfileSyncTimer = null;
    return;
  }
  chengfangProfileSyncTimer = setTimeout(() => {
    syncChengfangProfileToAgent(scope).catch((error) => {
      const saveStatus = document.getElementById("chengfang-profile-save-status");
      if (saveStatus) {
        saveStatus.className = "danger";
        saveStatus.textContent = error?.message || "保存失败，请检查本地 Agent";
      }
    });
  }, 600);
}

async function evaluateChengfangNow(trigger = "manual") {
  const scope = currentChengfangProfileScope();
  const request = chengfangScopedPostPayload({ profile: buildChengfangAgentProfile(scope), trigger }, scope);
  const result = await bridgeFetch("/chengfang/autopilot/evaluate", {
    method: "POST",
    body: JSON.stringify(request.body),
  });
  validateChengfangScopedResponse(scope, result);
  renderChengfangAgentRuntime(result.autopilot_runtime || {});
  renderChengfangPlanner();
  return result;
}

function renderChengfangDecision(decision) {
  const node = document.getElementById("chengfang-decision");
  node.className = `chengfang-decision ${decision.level || "warning"}`;
  document.getElementById("chengfang-today-conclusion").textContent = decision.conclusion;
  document.getElementById("chengfang-today-action").textContent = decision.action;
  document.getElementById("chengfang-today-why").textContent = decision.why;
  document.getElementById("chengfang-next-step").textContent = decision.next_step || currentChengfangGate.next_step;
}

function renderChengfangShadow(planner, decision) {
  const now = Date.now();
  const shadow = planner.buildShadowProgram(currentChengfangLocalPlan.shadow || {}, now);
  currentChengfangLocalPlan.shadow = shadow;
  const agentKnown = Boolean(currentChengfangAgentRuntime);
  const agentShadow = currentChengfangAgentRuntime?.shadow || {};
  const agentRunning = currentChengfangAgentRuntime?.decision_automation?.running === true;
  const active = agentKnown ? agentRunning : shadow.status === "active";
  const stateNode = document.getElementById("chengfang-shadow-state");
  const summaryNode = document.getElementById("chengfang-shadow-empty");
  const readbackNode = document.getElementById("chengfang-shadow-readbacks");
  const toggle = document.getElementById("chengfang-shadow-toggle");
  toggle.disabled = !active && !chengfangShadowSetupReady;
  if (active) {
    const startedAt = Number(agentShadow.started_at || shadow.started_at || now);
    const day = Math.min(7, Math.max(1, Math.floor((now - startedAt) / 86400000) + 1));
    stateNode.textContent = `第 ${day} / 7 天 · A1 运行中`;
    toggle.textContent = "停止影子自动评估";
    const latest = shadow.days.at(-1);
    const agentLatest = currentChengfangAgentRuntime?.latest_evaluation;
    summaryNode.textContent = agentLatest?.recommendation
      ? `${agentLatest.recommendation} Agent 已保存 ${Number(currentChengfangAgentRuntime.evaluation_count || 0)} 次去重评估；不会执行。`
      : latest
      ? `今日建议：${latest.recommendation}。共保存 ${shadow.days.length} 天记录；不会执行。`
      : `今日结论尚不足以生成建议：${decision.action}。不会执行。`;
    const slots = latest?.readbacks || { "2h": null, "24h": null, "3d": null, "7d": null };
    readbackNode.replaceChildren(...Object.entries(slots).map(([label, value]) => {
      const item = document.createElement("div");
      const strong = document.createElement("strong"); strong.textContent = label;
      const span = document.createElement("span"); span.textContent = value ? "已记录" : "待回读";
      item.append(strong, span); return item;
    }));
  } else {
    stateNode.textContent = shadow.status === "completed" ? "7 天已完成" : agentKnown ? "Agent 未运行" : "未开启";
    toggle.textContent = shadow.status === "completed" ? "重新开启 7 天观察" : "开启 7 天影子观察";
    summaryNode.textContent = chengfangShadowSetupReady
      ? "开启后每天访问或同步本页时保存一条只读建议，并预留多时窗回读。"
      : "先选择经营目标并补齐成本口径和经营边界，再开启观察。";
    readbackNode.replaceChildren();
  }
}

function renderChengfangFoundation(planner, calculation) {
  const evidence = readChengfangEvidenceInputs();
  const diagnosticInput = {
    ...evidence,
    contribution_margin: calculation.status === "ready" ? calculation.contribution_margin : "",
    refund_rate: calculation.parsed?.refund_rate?.status === "present" ? calculation.parsed.refund_rate.value : "",
  };
  const qualification = planner.assessQualification(diagnosticInput);
  const boundaries = planner.validateBoundaries(readChengfangBoundaryInputs());
  const goal = document.querySelector('input[name="chengfang-goal"]:checked')?.value || "";
  const profileSteps = [currentChengfangGate.identity_ready && currentChengfangGate.data_ready, Boolean(goal), calculation.status === "ready", boundaries.status === "ready"];
  document.getElementById("chengfang-profile-progress").textContent = `${profileSteps.filter(Boolean).length} / 4 完成`;
  document.querySelectorAll("[data-chengfang-boundary]").forEach((input) => input.classList.toggle("invalid", boundaries.invalid.includes(input.dataset.chengfangBoundary)));
  const boundaryStatus = document.getElementById("chengfang-boundary-status");
  boundaryStatus.textContent = boundaries.status === "ready" ? "经营边界已保存，仅用于本地判断。" : boundaries.status === "invalid" ? `存在 ${boundaries.invalid.length} 项无效边界，请检查红色字段。` : `还缺 ${boundaries.missing.length} 项经营边界。`;
  boundaryStatus.className = boundaries.status === "ready" ? "safe" : boundaries.status === "invalid" ? "danger" : "warning";
  chengfangShadowSetupReady = Boolean(goal) && calculation.status === "ready" && boundaries.status === "ready";
  renderChengfangCandidatePath(calculation, boundaries);
  const eligibilityNode = document.getElementById("chengfang-eligibility-empty");
  eligibilityNode.replaceChildren(...qualification.dimensions.map((dimension) => {
    const row = document.createElement("div"); row.className = "chengfang-dimension";
    const label = document.createElement("span"); label.textContent = dimension.label;
    const value = document.createElement("b"); value.textContent = dimension.score === null ? "数据不足" : `${dimension.score} 分`;
    row.title = dimension.evidence; row.append(label, value); return row;
  }));
  if (qualification.score !== null) {
    const total = document.createElement("p"); total.textContent = `证据完整后综合资格分：${qualification.score}。分数只用于排序，不代表平台准入。`;
    eligibilityNode.prepend(total);
  }
  const bottlenecks = planner.identifyBottlenecks(diagnosticInput);
  const bottleneckNode = document.getElementById("chengfang-bottleneck-empty");
  if (bottlenecks.status === "insufficient") {
    bottleneckNode.textContent = `数据不足：${bottlenecks.next_steps.join("、")}。当前不输出瓶颈结论。`;
  } else if (bottlenecks.status === "clear") {
    bottleneckNode.textContent = "当前规则未发现明确瓶颈；这不等于可以扩量，仍需持续观察。";
  } else {
    bottleneckNode.replaceChildren(...[bottlenecks.primary, ...bottlenecks.secondary].map((item, index) => {
      const row = document.createElement("p"); row.textContent = `${index ? "次要" : "主要"}：${item.title}（${item.evidence_level}）— ${item.evidence}；${item.action}`; return row;
    }));
  }
  const decision = planner.buildDecisionBrief({ goal, calculation, boundaries, qualification, bottlenecks, readiness: currentChengfangGate });
  renderChengfangDecision(decision);
  renderChengfangShadow(planner, decision);
  return { qualification, bottlenecks, boundaries, decision };
}

function money(value) {
  return `¥${Number(value).toFixed(2)}`;
}

function renderChengfangPlanner() {
  const planner = globalThis.DianChengfangPlanner;
  const resultNode = document.getElementById("chengfang-calculation-result");
  const scenarioNode = document.getElementById("chengfang-scenarios");
  const result = planner.calculate(readChengfangCalculatorInputs());
  document.querySelectorAll("[data-chengfang-input]").forEach((input) => {
    input.classList.toggle("invalid", result.invalid?.includes(input.dataset.chengfangInput));
  });
  if (result.status === "incomplete") {
    resultNode.className = "chengfang-calculation-result empty";
    resultNode.textContent = `还缺 ${result.missing.length} 项。空值不会按 0 处理；没有数据时不输出保本结论。`;
  } else if (result.status === "invalid") {
    resultNode.className = "chengfang-calculation-result danger";
    resultNode.textContent = result.reason || "存在无效输入，请检查红色标记字段。";
  } else if (result.status === "loss_before_ads") {
    resultNode.className = "chengfang-calculation-result danger";
    resultNode.textContent = `暂不适合投放：退款后预计收入 ${money(result.retained_revenue)}，非广告成本 ${money(result.non_ad_cost)}，未投广告前已无正向空间。`;
  } else {
    resultNode.className = "chengfang-calculation-result";
    resultNode.replaceChildren();
    const metrics = document.createElement("div");
    metrics.className = "chengfang-result-metrics";
    [["退款后预计收入", money(result.retained_revenue)], ["单件最高可承受广告消耗", money(result.max_ad_spend)], ["当前贡献毛利", money(result.contribution_margin)], ["测算保本 ROI", result.break_even_roi.toFixed(2)]]
      .forEach(([label, value]) => { const item = document.createElement("div"); const span = document.createElement("span"); const strong = document.createElement("strong"); span.textContent = label; strong.textContent = value; item.append(span, strong); metrics.append(item); });
    const note = document.createElement("p"); note.textContent = result.assumptions; note.style.marginBottom = "0";
    resultNode.append(metrics, note);
  }
  const scenarios = planner.buildScenarios(result);
  if (!scenarios.length) {
    scenarioNode.className = "chengfang-scenarios empty-state";
    scenarioNode.textContent = "完成有效测算后显示保守、基准和进取三档假设。";
  } else {
    scenarioNode.className = "chengfang-scenarios";
    scenarioNode.replaceChildren(...scenarios.map((scenario) => {
      const card = document.createElement("article"); card.className = "chengfang-scenario";
      const title = document.createElement("strong"); title.textContent = `${scenario.label}方案`;
      const value = document.createElement("b"); value.textContent = `参考 ROI ≥ ${scenario.target_roi.toFixed(2)}`;
      const spend = document.createElement("p"); spend.textContent = `单件测算广告空间：${money(scenario.max_ad_spend)}。${scenario.risk}`;
      card.append(title, value, spend); return card;
    }));
  }
  const foundation = renderChengfangFoundation(planner, result);
  return { calculation: result, ...foundation };
}

async function saveChengfangLocalPlan() {
  const scope = chengfangPlanScope(selectedStoreKey, selectedQianchuanAccount);
  if (!scope.scope_key || currentChengfangLocalPlan.scope_key !== scope.scope_key) {
    const saveStatus = document.getElementById("chengfang-profile-save-status");
    if (saveStatus) {
      saveStatus.className = "warning";
      saveStatus.textContent = "请先确认当前店铺与一个千川账户；未保存未归属的建档。";
    }
    return false;
  }
  const goal = document.querySelector('input[name="chengfang-goal"]:checked')?.value || "";
  const inputs = readChengfangCalculatorInputs();
  const evidence = readChengfangEvidenceInputs();
  const boundaries = readChengfangBoundaryInputs();
  currentChengfangLocalPlan = {
    ...currentChengfangLocalPlan,
    schema_version: CHENGFANG_LOCAL_PLAN_SCHEMA_VERSION,
    goal,
    inputs,
    evidence,
    boundaries,
    scope: { store_key: scope.store_key, qianchuan_account_id: scope.qianchuan_account_id },
    scope_key: scope.scope_key,
    saved_at: Date.now(),
    local_only: true,
    execution_allowed: false,
  };
  const rendered = renderChengfangPlanner();
  if (currentChengfangLocalPlan.shadow?.enabled) {
    currentChengfangLocalPlan.shadow = globalThis.DianChengfangPlanner.appendDailyShadow(currentChengfangLocalPlan.shadow, {
      date: localDateKey(), goal, recommendation: rendered.decision.action, evidence: rendered.decision.evidence,
    });
    renderChengfangShadow(globalThis.DianChengfangPlanner, rendered.decision);
  }
  currentChengfangLocalPlanStore = upsertChengfangPlanForScope(currentChengfangLocalPlanStore, currentChengfangLocalPlan);
  await chrome.storage.local.set({ [CHENGFANG_LOCAL_PLAN_KEY]: currentChengfangLocalPlanStore });
  const activeScope = chengfangPlanScope(selectedStoreKey, selectedQianchuanAccount);
  if (activeScope.scope_key === scope.scope_key && currentChengfangLocalPlan.scope_key === scope.scope_key) {
    scheduleChengfangProfileSync();
  }
  return true;
}

function restoreChengfangLocalPlan(saved = {}, options = {}) {
  const planner = globalThis.DianChengfangPlanner;
  const scope = options.scope
    ? chengfangPlanScope(options.scope.store_key, options.scope.qianchuan_account_id)
    : chengfangPlanScope(selectedStoreKey, selectedQianchuanAccount);
  const scopedSaved = saved && typeof saved === "object" && saved.scope_key === scope.scope_key
    ? saved
    : {};
  currentChengfangLocalPlan = {
    ...emptyChengfangLocalPlan(scope),
    ...scopedSaved,
    schema_version: CHENGFANG_LOCAL_PLAN_SCHEMA_VERSION,
    inputs: scopedSaved.inputs && typeof scopedSaved.inputs === "object" ? scopedSaved.inputs : {},
    boundaries: scopedSaved.boundaries && typeof scopedSaved.boundaries === "object" ? scopedSaved.boundaries : {},
    evidence: scopedSaved.evidence && typeof scopedSaved.evidence === "object" ? scopedSaved.evidence : {},
    shadow: scopedSaved.shadow && typeof scopedSaved.shadow === "object" ? scopedSaved.shadow : { enabled: false },
    scope: { store_key: scope.store_key, qianchuan_account_id: scope.qianchuan_account_id },
    scope_key: scope.scope_key,
    local_only: true,
    execution_allowed: false,
  };
  const goal = planner.GOALS[scopedSaved.goal] ? scopedSaved.goal : "";
  document.querySelectorAll('input[name="chengfang-goal"]').forEach((input) => { input.checked = false; });
  if (goal) document.querySelector(`input[name="chengfang-goal"][value="${goal}"]`).checked = true;
  document.getElementById("chengfang-goal-status").textContent = goal ? `已选择“${planner.GOALS[goal]}”；偏好仅保存在本机。` : "请选择目标；这里只保存偏好，不会修改乘方策略。";
  document.querySelectorAll("[data-chengfang-input]").forEach((input) => {
    const value = scopedSaved.inputs?.[input.dataset.chengfangInput];
    input.value = value === 0 || value ? String(value) : "";
  });
  document.querySelectorAll("[data-chengfang-evidence]").forEach((input) => {
    const value = scopedSaved.evidence?.[input.dataset.chengfangEvidence];
    input.value = value === 0 || value ? String(value) : "";
  });
  document.querySelectorAll("[data-chengfang-boundary]").forEach((input) => {
    const value = scopedSaved.boundaries?.[input.dataset.chengfangBoundary];
    input.value = value === 0 || value ? String(value) : "";
  });
  const rendered = renderChengfangPlanner();
  if (currentChengfangLocalPlan.shadow?.enabled) {
    renderChengfangShadow(planner, rendered.decision);
  }
  return currentChengfangLocalPlan;
}

function switchChengfangLocalPlanScope(storeKey = "", qianchuanAccountId = "") {
  const scope = chengfangPlanScope(storeKey, qianchuanAccountId);
  if (currentChengfangLocalPlan.scope_key === scope.scope_key) return currentChengfangLocalPlan;
  clearTimeout(chengfangProfileSyncTimer);
  chengfangProfileSyncTimer = null;
  currentChengfangAgentRuntime = null;
  currentChengfangA2Pilot = null;
  currentChengfangCandidatePath = null;
  const saved = chengfangPlanForScope(currentChengfangLocalPlanStore, scope);
  return restoreChengfangLocalPlan(saved || {}, { scope });
}

function renderOperationContext(payload = {}) {
  currentOperationContext = payload;
  renderPriorityReminder();
}

function renderOnboarding(payload = {}) {
  currentOnboarding = payload;
  renderSimpleJourney(payload, currentConnectionGuide || {});
}

function renderConnectionGuide(payload = {}, catalog = {}) {
  currentConnectionGuide = payload;
  const guide = document.getElementById("connection-guide");
  const guideView = globalThis.DianConnectionGuidePolicy.guideView(payload, { qianchuanDeferred: qianchuanFeatureDeferred });
  const collapsed = guideView.collapsed;
  guide.className = `connection-guide ${collapsed ? "collapsed" : "expanded"}`;
  guide.dataset.operationalState = guideView.operationalState || "unknown";
  guide.dataset.currentlyReady = String(guideView.currentlyReady);
  document.getElementById("connection-status-strip").hidden = !collapsed;
  document.getElementById("connection-guide-expanded").hidden = collapsed;
  const operationalSuffix = guideView.operationalLabel ? ` · ${guideView.operationalLabel}` : "";
  document.getElementById("connection-level").textContent = `${payload.level || "L0"} · ${payload.level_label || "尚未连接店铺"}${operationalSuffix}`;
  document.getElementById("connection-level-compact").textContent = `${payload.level || "L0"} · ${payload.level_label || "尚未连接"}${operationalSuffix}`;
  const updatedAt = Number(payload.store?.updated_at || 0);
  document.getElementById("connection-update-time").textContent = updatedAt
    ? `数据更新于 ${new Date(updatedAt * 1000).toLocaleString()}` : "暂无更新时间";
  document.getElementById("connection-center-freshness").textContent = updatedAt
    ? `${guideView.operationalLabel || "最近同步"} · ${new Date(updatedAt * 1000).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })}`
    : guideView.operationalLabel || "暂无数据";
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
  const unlinked = catalog.unlinked_accounts || [];
  if (globalThis.DianConnectionGuidePolicy.bindingReview({
    selectedStoreKey,
    unlinkedAccounts: unlinked,
    qianchuanDeferred: qianchuanFeatureDeferred,
    currentActionId: next.id,
  })) {
    guide.className = "connection-guide expanded";
    document.getElementById("connection-status-strip").hidden = true;
    document.getElementById("connection-guide-expanded").hidden = false;
    storeControls.hidden = false;
    document.getElementById("store-link-review").hidden = false;
    document.getElementById("connection-next-title").textContent = "确认当前店铺对应的千川账户";
    document.getElementById("connection-next-detail").textContent = "系统不会按同名店铺猜测。请核对匿名账户证据后人工确认，完成后才能生成乘方作用域。";
    action.dataset.action = "review_account_link";
    action.textContent = "核对并确认账户关联";
    action.disabled = false;
  }
  renderSimpleJourney(payload.onboarding || currentOnboarding || {}, payload);
}

function renderTodayFocus(ops = {}) {
  const focus = ops.today_focus || {};
  const topThree = focus.top_three || [];
  const dataState = currentDashboardFailures.includes("今日任务")
    ? "failed"
    : currentJourneyCommand?.dataState || currentSimpleJourney?.data_state || "never";
  const unavailableCopy = {
    loading: "正在核对最新经营数据",
    never: "尚未同步，暂不判断今日任务",
    stale: "数据已过期，刷新后再生成今日任务",
    failed: "最近读取失败，修复后再生成今日任务",
  }[dataState];
  document.getElementById("today-three-summary").textContent = topThree.length
    ? topThree.map((item, index) => `${index + 1}. ${item.title || "经营任务"}`).join(" · ")
    : unavailableCopy || "当前没有证据充分的待处理任务";
  document.getElementById("today-max-risk").textContent = focus.max_risk?.title
    || unavailableCopy
    || "本轮未发现需要立即处理的高风险事项";
  const yesterday = focus.yesterday_result;
  document.getElementById("yesterday-action-result").textContent = yesterday
    ? `${yesterday.status_label || "已复盘"} · ${yesterday.plan_name || "受控动作"}`
    : "昨日暂无已完成复盘动作";
  const unsynced = focus.unsynced_data || [];
  document.getElementById("unsynced-data-summary").textContent = unsynced.length
    ? `${unsynced.length} 项：${unsynced.slice(0, 2).map((item) => item.label).join("、")}`
    : unavailableCopy || "关键经营数据已同步";
}

function renderProductCapability(inputs = {}) {
  const api = globalThis.DianProductCapability;
  const card = document.getElementById("product-capability-card");
  if (!api?.derive || !card) return null;
  const report = api.derive(inputs);
  card.dataset.level = report.level;
  document.getElementById("product-capability-level").textContent = report.level_label;
  document.getElementById("product-capability-ready-count").textContent = String(report.ready_count);
  const progress = document.getElementById("product-capability-progress");
  progress.setAttribute("aria-valuenow", String(report.ready_count));
  progress.querySelector("span").style.width = `${Math.round((report.ready_count / Math.max(1, report.total_stages)) * 100)}%`;

  const stageIcons = { ready: "✓", attention: "!", blocked: "!", inactive: "○" };
  document.getElementById("product-capability-stages").replaceChildren(...report.stages.map((item, index) => {
    const row = document.createElement("li");
    row.className = `product-capability-stage ${item.status}`;
    row.setAttribute("aria-label", `${item.label}：${item.status_label}。${item.summary || ""}`);
    const marker = document.createElement("i"); marker.textContent = item.status === "inactive" ? String(index + 1) : stageIcons[item.status];
    const copy = document.createElement("div");
    const label = document.createElement("strong"); label.textContent = item.label;
    const detail = document.createElement("small");
    detail.textContent = item.status === "ready" ? item.summary : item.evidence?.[0] || item.summary || "等待前一步";
    copy.append(label, detail);
    const state = document.createElement("b"); state.textContent = item.status_label;
    row.append(marker, copy, state);
    return row;
  }));

  const available = report.truth.available_now;
  const unavailable = report.truth.not_available_yet;
  document.getElementById("product-capability-truth").textContent = available.length
    ? `现在可用：${available.join("、")}。${unavailable[0] ? `尚未开放：${unavailable[0]}。` : ""}`
    : `当前尚未形成可用闭环。${unavailable[0] || "请先完成店铺与数据准备。"}`;

  const next = report.next_action;
  const nextStage = report.stages.find((item) => item.id === next.stage_id) || report.stages.find((item) => item.status !== "ready");
  const blocker = nextStage?.evidence?.[0];
  document.getElementById("product-capability-next-reason").textContent = [next.reason, blocker]
    .filter((value, index, values) => value && values.indexOf(value) === index)
    .join("；") || "按当前步骤补齐证据后再继续。";
  const button = document.getElementById("product-capability-next");
  button.textContent = next.label;
  const nextTarget = next.target === "connection-guide" ? "scan-card" : next.target;
  button.dataset.workspaceTarget = nextTarget;
  button.dataset.workspaceTitle = nextStage?.label || next.label;
  button.dataset.workspaceSubtitle = next.reason;
  const workspaceKeys = {
    "scan-card": "data-scan",
    "promotion-plan-center": "promotion-overview",
    "automation-section": "controlled-execution",
    "promotion-operation-log": "promotion-operation-log",
  };
  button.dataset.workspaceKey = workspaceKeys[nextTarget] || "";
  if (["promotion-plan-center", "automation-section", "promotion-operation-log"].includes(nextTarget)) button.dataset.workspaceRole = "直播投放";
  else delete button.dataset.workspaceRole;
  return report;
}

function journeyLaneForCurrentContext() {
  if (currentJourneyLane === "store" || currentJourneyLane === "ads") return currentJourneyLane;
  return currentRole === "直播投放" ? "ads" : "store";
}

function renderJourneyCommand(inputs = currentJourneyInputs || {}) {
  const api = globalThis.DianJourneyOrchestrator;
  const root = document.getElementById("journey-command-center");
  if (!api?.deriveJourney || !root) return null;
  currentJourneyInputs = inputs;
  const lane = journeyLaneForCurrentContext();
  let command = api.deriveJourney({ ...inputs, lane });
  if (command.state === "today_task_ready") {
    const commandTask = (inputs.ops?.all_tasks || inputs.ops?.today_top_actions || [])
      .find((item) => item?.status === "observing" && String(item.title || "") === String(command.title || ""));
    if (commandTask) {
      const observationAction = observationPrimaryAction(commandTask);
      command = {
        ...command,
        detail: observationAction.detail,
        observationTaskId: commandTask.id || "",
        primaryAction: {
          id: observationAction.due ? "readback_observation" : "review_observation",
          kind: "observation",
          label: observationAction.label,
          targetId: "today-task-center",
          detail: observationAction.detail,
        },
      };
    }
  }
  currentJourneyCommand = command;
  document.body.classList.add("journey-orchestrated");
  root.className = `journey-command-center ${command.status === "blocked" ? "blocked" : command.status === "complete" ? "complete" : command.status || "ready"}`;
  root.style.setProperty("--journey-stage-count", String(Math.max(1, command.stages.length)));

  document.querySelectorAll("[data-journey-lane]").forEach((button) => {
    const active = button.dataset.journeyLane === command.lane;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  const storeLabel = String(
    inputs.onboarding?.store_name
    || inputs.connectionGuide?.store_name
    || inputs.connection_guide?.store_name
    || (selectedStoreKey ? "抖店已准备" : "等待打开抖店"),
  );
  const accountKey = String(command.scope?.accountKey || selectedQianchuanAccount || "");
  document.getElementById("journey-scope-summary").textContent = command.lane === "ads"
    ? `${storeLabel} → ${accountKey ? `千川账户 ${accountKey.slice(-8)}` : "千川账户待选择"}`
    : `${storeLabel} → 店铺经营闭环（千川可选）`;

  document.getElementById("journey-stage-list").replaceChildren(...command.stages.map((item, index) => {
    const row = document.createElement("li");
    row.className = item.status;
    row.setAttribute("aria-current", item.status === "current" || item.status === "blocked" ? "step" : "false");
    row.title = item.summary || item.label;
    const marker = document.createElement("b");
    marker.textContent = item.status === "done" ? "✓" : item.status === "blocked" ? "!" : String(index + 1);
    const label = document.createElement("span"); label.textContent = item.label;
    row.append(marker, label);
    return row;
  }));

  const stateLabels = {
    offline: "连接中断",
    identity_blocked: "页面已切换",
    authorization_in_progress: "安全流程进行中",
    readback_in_progress: "等待结果验证",
    store_unconfirmed: "开始巡店",
    data_required: "数据准备",
    ads_account_blocked: "账户准备",
    ads_plan_data_required: "计划准备",
    ads_plan_identity_blocked: "计划信息待补齐",
    ads_boundary_blocked: "经营边界",
    today_task_ready: "现在只做这一件事",
    complete: "本轮闭环完成",
  };
  document.getElementById("journey-state-label").textContent = stateLabels[command.state] || "当前唯一下一步";
  document.getElementById("journey-command-title").textContent = command.title;
  document.getElementById("journey-command-detail").textContent = command.detail;
  const actionButton = document.getElementById("journey-primary-action");
  actionButton.textContent = command.primaryAction?.label || "暂无操作";
  actionButton.dataset.actionId = command.primaryAction?.id || "none";
  actionButton.dataset.actionKind = command.primaryAction?.kind || "none";
  actionButton.dataset.targetId = command.primaryAction?.targetId || "";
  actionButton.disabled = command.primaryAction?.disabled === true || command.primaryAction?.kind === "none";
  configureWorkspacePrimaryAction(currentWorkspaceTarget);

  const evidenceRows = [
    ...command.blockers.map((item) => ({ label: "阻断", value: item.message, status: "blocked" })),
    ...command.evidence.map((item) => ({ label: item.label, value: item.value, status: item.status })),
  ];
  const evidenceList = document.getElementById("journey-evidence-list");
  evidenceList.replaceChildren(...evidenceRows.map((item) => {
    const row = document.createElement("li");
    row.className = item.status || "info";
    const label = document.createElement("strong"); label.textContent = `${item.label}：`;
    row.append(label, document.createTextNode(item.value || "暂无证据"));
    return row;
  }));
  document.getElementById("journey-evidence").open = command.status === "blocked" && command.blockers.length > 0;
  return command;
}

async function runJourneyPrimaryAction() {
  const command = currentJourneyCommand;
  const selectedAction = command?.primaryAction;
  const button = document.getElementById("journey-primary-action");
  if (!selectedAction || selectedAction.disabled || selectedAction.kind === "none") return;
  button.disabled = true;
  const idleLabel = selectedAction.label;
  button.textContent = "正在处理…";
  try {
    if (selectedAction.kind === "observation") {
      const observationButton = [...document.querySelectorAll("[data-observation-primary='true']")]
        .find((item) => item.dataset.observationTaskId === command.observationTaskId);
      if (observationButton) observationButton.click();
      else navigateToWorkspaceElement("today-task-center", { workspaceKey: "today-tasks", highlight: true });
      return;
    }
    if (selectedAction.kind === "repair_agent") {
      await openAgentRepairGuide();
      button.textContent = "修复指引已打开";
      return;
    }
    if (selectedAction.kind === "scan") {
      navigateToWorkspaceElement("scan-card", { workspaceKey: "data-scan" });
      const requestedPageIds = Array.isArray(selectedAction.pageIds) ? selectedAction.pageIds : [];
      if (selectedAction.id === "refresh_core_data" && !requestedPageIds.length) {
        throw new Error("尚未取得精确待刷新页面，已停止扩大扫描范围。请刷新后重试。");
      }
      await runQuickScan(button, {
        pageIds: requestedPageIds.length ? requestedPageIds : QUICK_SCAN_PAGE_IDS,
        purpose: selectedAction.id === "refresh_core_data" ? "refresh_core_data" : "quick_scan",
      });
      return;
    }
    if (selectedAction.kind === "sync_ads") {
      navigateToWorkspaceElement("promotion-plan-center", { role: "直播投放", workspaceKey: "promotion-overview" });
      await syncRecentQianchuanPage({
        expectedPageTypes: ["campaigns", "qianchuan_campaigns", "qianchuan_live"],
        purpose: "投放计划同步",
      });
      return;
    }
    const targetId = selectedAction.targetId || "today-task-center";
    const adsTarget = command.lane === "ads" || ["promotion-plan-center", "automation-section", "promotion-operation-log"].includes(targetId);
    navigateToWorkspaceElement(targetId, {
      role: adsTarget ? "直播投放" : "",
      workspaceKey: {
        "scan-card": "data-scan",
        "today-task-center": "today-tasks",
        "promotion-plan-center": "promotion-overview",
        "automation-section": "controlled-execution",
        "promotion-operation-log": "promotion-operation-log",
      }[targetId] || "",
      focusElement: null,
      highlight: true,
    });
  } catch (error) {
    document.getElementById("journey-command-detail").textContent = error?.message || "当前步骤处理失败，请根据证据提示重试。";
    button.textContent = "重试当前步骤";
  } finally {
    if (button.textContent === "正在处理…") button.textContent = idleLabel;
    button.disabled = false;
  }
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
    } else {
      // Always replace the previous store's DOM. Leaving this branch empty
      // could show stale health cards after switching to a store with no
      // baselines yet.
      container.className = "stack empty-state";
      container.textContent = "当前店铺尚未建立采集基线；完成至少 5 次巡店后开始监测。";
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
  const rate = Number(report.effective_rate || 0);
  const rateEl = document.getElementById("effectiveness-rate");
  const container = document.getElementById("effectiveness-detail");
  if (!report.total_tracked) {
    rateEl.textContent = "暂无数据";
    container.className = "stack empty-state";
    container.textContent = "开始处理任务时会自动建立基线；操作完成后进入效果观察，再填写结果结案。";
    return;
  }
  const comparable = Number(report.comparable_count || 0);
  rateEl.textContent = comparable ? `${rate}% 自动有效` : "等待可比较样本";
  container.className = "stack";
  const summary = document.createElement("p");
  summary.textContent = comparable
    ? `跨日跟踪 ${report.total_tracked} 条：自动可比较 ${comparable} 条，其中有效 ${report.effective_count} 条、无效 ${report.ineffective_count || 0} 条。`
    : `跨日跟踪 ${report.total_tracked} 条：待结案 ${report.pending_count || 0} 条、人工结案 ${report.manual_verified_count || 0} 条、证据不足 ${report.inconclusive_count || 0} 条。`;
  container.replaceChildren(summary);
  if (report.manual_verified_count || report.inconclusive_count || report.pending_count) {
    const note = document.createElement("small");
    note.textContent = `人工结案 ${report.manual_verified_count || 0} · 证据不足 ${report.inconclusive_count || 0} · 待结案 ${report.pending_count || 0}（这些不计入自动有效率）`;
    container.append(note);
  }
  const recent = report.recent_evaluations || [];
  if (recent.length) {
    const heading = document.createElement("small"); heading.textContent = "最近评估：";
    container.append(heading);
    recent.slice(0, 5).forEach((item) => {
      const row = document.createElement("div"); row.className = "eval-row";
      const tag = document.createElement("span");
      const labels = { effective: "自动有效", ineffective: "自动无效", manual_verified: "人工结案", inconclusive: "证据不足" };
      tag.textContent = labels[item.status] || "待评估";
      tag.className = `eval-tag ${item.status === "effective" ? "ok" : item.status === "ineffective" ? "warn" : ""}`;
      const detail = document.createElement("small");
      const changes = (item.changes || []).slice(0, 2).map((change) => `${change.metric}: ${change.old}→${change.new}`).join(", ");
      detail.textContent = changes || item.completion_note || (item.status === "manual_verified" ? "暂无结案后的新快照" : "暂无可比较经营指标");
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
      if (item.status === "preflight_ready") markAgentWriteControl(button);
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
            navigateToWorkspaceElement("automation-section", {
              role: "直播投放", workspaceKey: "controlled-execution",
              focusElement: document.getElementById("preflight-state"),
            });
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
  const archivedReadbacks = (Array.isArray(report.recoverable_readback_actions)
    ? report.recoverable_readback_actions
    : []).filter((item) => item && typeof item === "object");
  currentPreflightState = state;
  currentPreflightSession = report.session || null;
  currentPreflightAction = report.action || null;
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
  panel.hidden = state === "idle" && archivedReadbacks.length === 0;
  panel.className = `execution-preflight${state === "ready_for_final_confirmation" ? " ready" : ["blocked", "expired", "manual_reconcile_required"].includes(state) ? " blocked" : state === "manual_reconcile_archived" ? " archived" : state === "stopped" ? " stopped" : ""}`;
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
  const archivedHub = document.getElementById("archived-readback-hub");
  const archivedList = document.getElementById("archived-readback-list");
  const otherArchivedReadbacks = archivedReadbacks.filter(
    (item) => String(item.action_id || "") !== String(currentPreflightSession?.action_id || ""),
  );
  archivedHub.hidden = otherArchivedReadbacks.length === 0;
  document.getElementById("archived-readback-count").textContent = `${otherArchivedReadbacks.length} 项`;
  archivedList.replaceChildren(...otherArchivedReadbacks.map((item) => {
    const row = document.createElement("article");
    row.className = "archived-readback-item";
    const body = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = item.plan_name || item.plan_id || "归档计划";
    const detail = document.createElement("small");
    detail.textContent = `${item.account_key || "原千川账户"} · 结果仍未知 · 同计划禁止自动重投`;
    body.append(title, detail);
    const retry = document.createElement("button");
    retry.type = "button";
    retry.textContent = "继续只读核验";
    retry.addEventListener("click", async () => {
      retry.disabled = true;
      retry.textContent = "正在核验…";
      try {
        const result = await chrome.runtime.sendMessage({
          type: "manual-execution-readback",
          action_id: String(item.action_id || ""),
        });
        if (!result?.ok) throw new Error(result?.error || "独立回读失败");
        await refreshExecutionPreflight();
        document.getElementById("preflight-next").textContent = result.verification?.verified
          ? "归档动作已由新的独立页面确认生效；同计划永久重投锁仍保留。"
          : "已完成一次新的独立页面回读；结果仍不确定，未触发任何投放操作。";
      } catch (error) {
        document.getElementById("preflight-next").textContent = error.message || "独立回读失败；归档动作继续锁定。";
      } finally {
        retry.disabled = false;
        retry.textContent = "继续只读核验";
      }
    });
    row.append(body, retry);
    return row;
  }));
  const reread = document.getElementById("preflight-reread");
  const authorize = document.getElementById("preflight-authorize");
  const archive = document.getElementById("preflight-archive");
  const stop = document.getElementById("preflight-stop");
  reread.hidden = ["ready_for_final_confirmation", "authorized", "stopped"].includes(state);
  reread.disabled = state === "expired";
  authorize.hidden = state !== "ready_for_final_confirmation";
  authorize.dataset.targetValue = action.target_value ?? "";
  authorize.dataset.operationType = action.operation_type || "adjust_budget";
  authorize.dataset.confirmationText = action.confirmation_text || "";
  archive.hidden = state !== "manual_reconcile_required" || action.manual_reconcile_archivable !== true;
  archive.disabled = archive.hidden || !currentPreflightSession;
  archive.dataset.confirmationText = action.archive_confirmation_text || "";
  stop.disabled = !currentPreflightSession || ["stopped", "expired", "authorization_consumed", "manual_reconcile_required", "manual_reconcile_archived", "completed", "execution_failed"].includes(state);
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
    const needsAttention = ["ineffective", "rollback_recommended", "needs_reread"].includes(item.status);
    card.className = `shadow-card${item.status === "effective" ? " matched" : needsAttention ? " attention" : ""}`;
    const head = document.createElement("div"); head.className = "card-title";
    const title = document.createElement("strong"); title.textContent = item.plan_name || "千川计划";
    const tag = document.createElement("span"); tag.className = "shadow-status"; tag.textContent = item.status_label || "待复查";
    head.append(title, tag);
    const change = document.createElement("p"); change.className = "shadow-change";
    change.textContent = `预算 ${item.change?.from ?? "--"} → ${item.change?.to ?? "--"}`;
    const metrics = document.createElement("small");
    metrics.textContent = item.after
      ? `调整前 ROI ${item.before?.roi ?? "--"} / 订单 ${item.before?.orders ?? "--"}；最新 ROI ${item.after?.roi ?? "--"} / 订单 ${item.after?.orders ?? "--"}；新增消耗 ${item.incremental_spend ?? "--"} 元（最小样本 ${item.minimum_sample_spend ?? "--"} 元）`
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
      markAgentWriteControl(rollback);
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
          navigateToWorkspaceElement("automation-section", {
            role: "直播投放", workspaceKey: "controlled-execution",
            focusElement: document.getElementById("preflight-state"),
          });
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
    summary.rollback_recommended
      ? `${summary.rollback_recommended} 项建议恢复`
      : summary.ineffective
        ? `${summary.ineffective} 项未生效`
        : summary.needs_reread
          ? `${summary.needs_reread} 项待读取`
          : `${items.length} 项`;
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
        applied.dataset.taskWrite = "manual-applied";
        markAgentWriteControl(applied);
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
            const response = await chrome.runtime.sendMessage({
              type: "sync-current-qianchuan",
              expected_account_key: selectedQianchuanAccount,
              expected_store_key: selectedStoreKey,
              purpose: "execution_effect_readback",
            });
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

async function reportExtensionInstallSource() {
  const manifest = chrome.runtime.getManifest();
  const source = DianBridgeAuth.extensionSourcePayload({
    manifest,
    extensionId: chrome.runtime.id || "",
    userAgent: navigator.userAgent || "",
  });
  return bridgeFetch("/distribution/extension-source", {
    method: "POST",
    body: JSON.stringify(source),
  });
}

function renderReleaseCheck(checks, id, stateId, noteId, readyText, blockedText) {
  const check = checks.find((item) => item?.id === id) || {};
  const state = document.getElementById(stateId);
  state.className = `status-dot ${check.ready ? "ready" : "blocked"}`;
  document.getElementById(noteId).textContent = check.ready ? readyText : blockedText;
}

function industryPackRef(pack = {}) {
  return `${String(pack.pack_id || "general")}|${String(pack.pack_version || "")}`;
}

function currentIndustryPackSelection() {
  const value = document.getElementById("industry-pack-select").value;
  return (currentKnowledgeCatalog?.packs || []).find((pack) => industryPackRef(pack) === value) || null;
}

function renderIndustryPackPreview(pack = {}, knowledge = currentKnowledgeCatalog || {}) {
  const preview = document.getElementById("industry-pack-preview");
  const title = preview.querySelector("strong");
  title.textContent = pack.display_name || "通用电商经营知识包";
  const version = pack.pack_version ? `v${pack.pack_version}` : "内置基础版";
  document.getElementById("industry-pack-rule-summary").textContent = `${version} · ${Number(pack.rule_count || 0)} 条本地规则 · ${pack.source === "installed" ? "已验签安装" : "内置可用"}`;
  document.getElementById("industry-pack-capabilities").textContent = Array.isArray(pack.capabilities) && pack.capabilities.length
    ? pack.capabilities.join(" · ")
    : "数据时效 · 投放止损 · 库存预警";
  const applyButton = document.getElementById("activate-industry-pack");
  const storeReady = Boolean(knowledge.store_key || selectedStoreKey);
  const binding = knowledge.binding && typeof knowledge.binding === "object" ? knowledge.binding : null;
  const hasIndustryBinding = Boolean(binding && String(binding.pack_id || "general") !== "general");
  const isGeneral = String(pack.pack_id || "general") === "general";
  const isActive = isGeneral
    ? !hasIndustryBinding
    : !knowledge.fallback_reason
      && String(pack.pack_id || "") === String(knowledge.active_pack_id || "")
      && String(pack.pack_version || "") === String(binding?.pack_version || knowledge.version || "");
  applyButton.disabled = !storeReady || pack.compatible === false || isActive;
  applyButton.textContent = isActive ? "当前店铺已应用" : "应用到当前店铺";
}

function renderIndustryPackCenter(knowledge = {}) {
  currentKnowledgeCatalog = knowledge;
  const packs = Array.isArray(knowledge.packs) ? knowledge.packs : [];
  const select = document.getElementById("industry-pack-select");
  select.textContent = "";
  packs.forEach((pack) => {
    const option = document.createElement("option");
    option.value = industryPackRef(pack);
    const unavailable = pack.compatible === false ? ` · 不可用：${String(pack.reason || "校验失败").slice(0, 48)}` : "";
    option.textContent = `${pack.display_name || pack.industry_label || pack.pack_id} · ${pack.pack_version ? `v${pack.pack_version}` : "基础版"}${pack.source === "installed" ? " · 已安装" : " · 内置"}${unavailable}`;
    option.title = pack.compatible === false ? String(pack.reason || "该知识包当前不可用") : "";
    option.disabled = pack.compatible === false;
    select.appendChild(option);
  });
  if (!packs.length) {
    const option = document.createElement("option");
    option.value = "general|";
    option.textContent = "通用电商经营知识包";
    select.appendChild(option);
  }
  const active = packs.find((pack) => pack.selected) || packs.find((pack) => pack.pack_id === "general") || {};
  const activeRef = industryPackRef(active);
  if ([...select.options].some((option) => option.value === activeRef)) select.value = activeRef;
  const storeKey = String(knowledge.store_key || selectedStoreKey || "");
  const stores = Array.isArray(currentQianchuanCatalog?.stores) ? currentQianchuanCatalog.stores : [];
  const store = stores.find((item) => String(item.key || "") === storeKey);
  document.getElementById("industry-pack-store-name").textContent = storeKey
    ? `${store?.label || "当前已选店铺"} · 仅本店生效`
    : "等待打开抖店；不同抖店不会混用规则";
  select.disabled = !storeKey;
  const trustState = document.getElementById("industry-pack-trust-state");
  const availableCount = Number(knowledge.available_count ?? knowledge.installed_count ?? packs.filter((pack) => pack.pack_id !== "general" && pack.compatible !== false).length);
  const invalidCount = Number(knowledge.invalid_count || 0);
  trustState.textContent = `${availableCount} 个行业包可选${invalidCount ? ` · ${invalidCount} 个不可用` : ""}`;
  trustState.className = knowledge.fallback_reason === "bound_pack_unavailable" || invalidCount ? "warn" : "";
  const message = document.getElementById("industry-pack-message");
  message.className = "industry-pack-message";
  if (!storeKey) {
    message.textContent = "打开抖店并完成一次巡店后，系统会自动保存该店的行业判断；此前继续使用通用电商规则。";
  } else if (knowledge.fallback_reason === "bound_pack_unavailable" || knowledge.fallback_reason === "bound_pack_merge_failed") {
    message.classList.add("warn");
    message.textContent = "原绑定知识包已失效、损坏或不兼容，当前已安全回退到通用电商规则；其他店铺不受影响。";
  } else if (String(knowledge.active_pack_id || "general") === "general") {
    message.textContent = "当前使用通用电商规则，可以正常巡检；选择主营行业后会增加更贴近类目的判断。";
  } else {
    message.textContent = `${active.display_name || "行业知识包"} 已在本店生效，共 ${Number(knowledge.rule_count || knowledge.effective_rule_count || 0)} 条通用与行业规则；下一步建议重新巡检。`;
  }
  renderIndustryPackPreview(active, knowledge);
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
  const platformLabel = system.platform === "macos" ? "macOS Developer ID 与公证" : "Windows Authenticode";
  document.getElementById("release-code-signature-label").textContent = platformLabel;
  renderReleaseCheck(checks, "platform_code_signature", "release-authenticode-state", "release-authenticode-note", "当前平台发布签名已确认", "当前平台签名或发行公证未完整");
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
  document.getElementById("industry-pack-state").textContent = `${knowledge.industry_label || industry} · ${formatVersion(knowledge.version)} · 本店${industry === "general" ? "通用" : "已启用"}`;
  renderIndustryPackCenter(knowledge);
  const importButton = document.getElementById("import-industry-pack");
  const importTrust = knowledge.local_import_trust_state || { ready: knowledge.local_import_trust_configured, error: "" };
  importButton.disabled = !knowledge.local_import_supported || !importTrust.ready;
  importButton.title = importTrust.ready
    ? "导入经过 Ed25519 验签的行业知识包"
    : `自定义导入不可用：${importTrust.error || "尚未配置行业知识包验签公钥"}`;
  if (!importTrust.ready) {
    const trustState = document.getElementById("industry-pack-trust-state");
    trustState.textContent = `${trustState.textContent} · 自定义导入未就绪`;
    trustState.classList.add("warn");
  }
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

function dashboardLoadIsCurrent(generation = 0) {
  return !generation || generation === dashboardLoadGeneration;
}

async function loadSystemStatus(generation = 0) {
  try {
    const system = await dashboardRead("/system/status", generation);
    if (!dashboardLoadIsCurrent(generation)) return null;
    renderSystemStatus(system);
    return system;
  } catch (error) {
    if (!dashboardLoadIsCurrent(generation)) return null;
    renderSystemStatus({ ready: false, update: { error: error.message || "版本状态读取失败" } });
    return null;
  }
}

const AI_PROVIDER_UI = Object.freeze({
  openai: { label: "GPT（OpenAI）", model: "例如：gpt-5-mini", baseUrl: "留空使用 OpenAI 默认地址", providerNote: "使用 OpenAI 官方模型；仅发送预览中列出的脱敏聚合数据。", baseUrlNote: "留空将使用 OpenAI 官方默认地址。" },
  deepseek: { label: "DeepSeek", model: "例如：deepseek-chat", baseUrl: "留空使用 DeepSeek 默认地址", providerNote: "使用 DeepSeek 官方 API；模型名称以已开通的控制台能力为准。", baseUrlNote: "留空将使用 DeepSeek 官方默认地址。" },
  qwen_bailian: {
    label: "千问 / 阿里云百炼",
    model: "默认：qwen-plus，也可填写已开通的千问模型",
    baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    providerNote: "使用阿里云百炼中的千问模型；业务空间如有专属地址，请按控制台填写。",
    baseUrlNote: "默认使用百炼 OpenAI 兼容地址；如业务空间提供独立官方地址，可替换为该官方地址。",
    defaultModel: "qwen-plus",
    defaultBaseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1",
  },
  glm_zhipu: {
    label: "智谱 GLM",
    model: "默认：glm-5.2，也可填写智谱控制台中已开通的模型",
    baseUrl: "https://open.bigmodel.cn/api/paas/v4",
    providerNote: "使用智谱开放平台 GLM 模型；模型权限与可用名称以控制台为准。",
    baseUrlNote: "默认使用智谱开放平台官方兼容地址。",
    defaultModel: "glm-5.2",
    defaultBaseUrl: "https://open.bigmodel.cn/api/paas/v4",
  },
  hunyuan_tencent: {
    label: "腾讯混元",
    model: "默认：hy3，也可按腾讯云 TokenHub 控制台填写",
    baseUrl: "https://tokenhub.tencentmaas.com/v1",
    providerNote: "使用腾讯云 TokenHub 接入混元；模型权限与可用名称以控制台为准。",
    baseUrlNote: "默认使用腾讯云 TokenHub；国际站/备用域名请严格按控制台填写。",
    defaultModel: "hy3",
    defaultBaseUrl: "https://tokenhub.tencentmaas.com/v1",
  },
  doubao_ark: {
    label: "豆包 / 火山引擎方舟",
    model: "默认：doubao-seed-2-0-lite-260215；也可填写控制台 Model/Endpoint ID",
    baseUrl: "https://ark.cn-beijing.volces.com/api/v3",
    providerNote: "使用火山方舟中的豆包模型；专属推理接入点请填写控制台提供的 Model/Endpoint ID。",
    baseUrlNote: "当前预设仅支持火山引擎方舟北京官方推理地址。",
    defaultModel: "doubao-seed-2-0-lite-260215",
    defaultBaseUrl: "https://ark.cn-beijing.volces.com/api/v3",
  },
  openai_compatible: { label: "OpenAI 兼容服务", model: "填写服务支持的模型名称", baseUrl: "例如：https://example.com/v1", providerNote: "高级接入：请同时核对服务商的模型名称、HTTPS 地址和数据政策。", baseUrlNote: "仅填写该服务商公布的 HTTPS 官方接口地址。" },
  local: { label: "本地模型", model: "例如：qwen3:8b", baseUrl: "例如：http://127.0.0.1:11434/v1", providerNote: "数据由本机模型处理；Agent 仍只接受结构化影子建议，不开放执行权限。", baseUrlNote: "本地服务请使用 127.0.0.1 或 localhost 地址。" },
});

function aiProviderDefinition(provider = "") {
  return AI_PROVIDER_UI[provider] || AI_PROVIDER_UI.openai;
}

function aiStatusView(status = {}) {
  const shadowRunning = Boolean(status.shadow_running || status.shadow?.running || status.mode === "shadow_running");
  const analysisAvailable = Boolean(
    status.analysis_available
    ?? status.connected
    ?? ["ready", "available", "connected", "shadow_running"].includes(String(status.state || "").toLowerCase())
  );
  if (shadowRunning) return { key: "shadow", label: "影子运行", analysisAvailable: true };
  if (analysisAvailable) return { key: "available", label: "分析可用", analysisAvailable: true };
  return { key: "disconnected", label: "AI未连接", analysisAvailable: false };
}

function updateAiProviderPlaceholders() {
  const provider = document.getElementById("ai-provider-select").value || "openai";
  const definition = aiProviderDefinition(provider);
  const modelField = document.getElementById("ai-model-input");
  const baseUrlField = document.getElementById("ai-base-url-input");
  modelField.placeholder = definition.model;
  baseUrlField.placeholder = definition.baseUrl;
  document.getElementById("ai-provider-note").textContent = definition.providerNote || "请选择模型服务并核对对应控制台配置。";
  document.getElementById("ai-base-url-note").textContent = definition.baseUrlNote || "留空将使用所选服务的默认地址。";
  const keyField = document.getElementById("ai-api-key-input");
  const remoteConsentField = document.getElementById("ai-remote-consent-field");
  const remoteConsent = document.getElementById("ai-remote-consent");
  remoteConsentField.hidden = provider === "local";
  if (provider === "local") remoteConsent.checked = false;
  if (provider === "local") {
    keyField.placeholder = "本地服务无需密钥时可留空";
  } else if (currentAiStatus.credential_saved || currentAiStatus.api_key_saved) {
    keyField.placeholder = "已安全保存；留空不会修改，页面不回显";
  } else {
    keyField.placeholder = "输入后仅发送给本地 Agent；保存后不回显";
  }
}

function applyAiProviderDefaults() {
  const provider = document.getElementById("ai-provider-select").value || "openai";
  const definition = aiProviderDefinition(provider);
  document.getElementById("ai-model-input").value = definition.defaultModel || "";
  document.getElementById("ai-base-url-input").value = definition.defaultBaseUrl || "";
  // Never carry a typed secret across providers or origins.
  document.getElementById("ai-api-key-input").value = "";
  document.getElementById("ai-remote-consent").checked = false;
  updateAiProviderPlaceholders();
}

function renderAiStatus(status = {}, { hydrate = true } = {}) {
  currentAiStatus = status && typeof status === "object" ? status : {};
  const view = aiStatusView(currentAiStatus);
  const state = document.getElementById("ai-connection-state");
  state.textContent = view.label;
  state.className = view.key;
  const provider = String(currentAiStatus.provider || currentAiStatus.provider_id || "openai");
  const providerSelect = document.getElementById("ai-provider-select");
  const selectedProvider = Object.hasOwn(AI_PROVIDER_UI, provider) ? provider : "openai_compatible";
  if (hydrate) {
    providerSelect.value = selectedProvider;
    document.getElementById("ai-model-input").value = String(currentAiStatus.model || "");
    document.getElementById("ai-base-url-input").value = String(currentAiStatus.base_url || "");
    document.getElementById("ai-remote-consent").checked = Boolean(currentAiStatus.remote_access_approved);
    // Secret values are deliberately never hydrated from any status/config response.
    document.getElementById("ai-api-key-input").value = "";
  }
  const definition = aiProviderDefinition(selectedProvider);
  const configured = Boolean(currentAiStatus.configured || currentAiStatus.credential_saved || currentAiStatus.api_key_saved || view.analysisAvailable);
  document.getElementById("ai-provider-summary").textContent = configured
    ? `${definition.label}${currentAiStatus.model ? ` · ${currentAiStatus.model}` : ""}`
    : "尚未配置模型";
  document.getElementById("ai-api-key-note").textContent = currentAiStatus.credential_saved || currentAiStatus.api_key_saved
    ? "密钥已由本地 Agent 保存；页面不会读取或显示，留空不会修改。"
    : selectedProvider === "local"
      ? "本地服务不需要密钥时可以留空；页面不会读取已保存凭证。"
      : "页面不会读取或显示已经保存的密钥。";
  document.getElementById("ai-run-shadow").disabled = !view.analysisAvailable;
  document.getElementById("ai-disable-connections").disabled = !(
    Array.isArray(currentAiStatus.providers) && currentAiStatus.providers.some((item) => item?.enabled)
  );
  updateAiProviderPlaceholders();
}

function redactAiSecrets(value = "") {
  let text = String(value || "");
  const typedSecret = document.getElementById("ai-api-key-input")?.value?.trim?.() || "";
  if (typedSecret) text = text.split(typedSecret).join("[密钥已隐藏]");
  return text
    .replace(/\bsk-[a-z0-9_-]{8,}\b/gi, "[密钥已隐藏]")
    .replace(/((?:api[ _-]?key|token|secret|密钥)\s*[:=：]\s*)[^\s,，;；]+/gi, "$1[已隐藏]")
    .slice(0, 600);
}

function setAiMessage(id, text, tone = "") {
  const node = document.getElementById(id);
  node.className = `ai-inline-message ${tone}`.trim();
  node.textContent = redactAiSecrets(text);
}

function aiProviderPayload() {
  const provider = document.getElementById("ai-provider-select").value || "openai";
  const payload = {
    provider,
    model: document.getElementById("ai-model-input").value.trim(),
    base_url: document.getElementById("ai-base-url-input").value.trim(),
    remote_access_approved: provider !== "local" && document.getElementById("ai-remote-consent").checked,
  };
  const apiKey = document.getElementById("ai-api-key-input").value.trim();
  if (apiKey) payload.api_key = apiKey;
  return payload;
}

function aiRemoteConsentReady(payload = {}) {
  return payload.provider === "local" || payload.remote_access_approved === true;
}

function safeAiPreviewItems(value, fallback = [], { excludeSecrets = false } = {}) {
  const items = Array.isArray(value) ? value : [];
  const secretPattern = /api[ _-]?key|cookie|token|secret|密码|密钥|登录凭证/i;
  const normalized = items.map((item) => {
    if (typeof item === "string") return item;
    if (item && typeof item === "object") return String(item.label || item.name || item.field || "");
    return "";
  }).map((item) => item.trim().slice(0, 100)).filter(Boolean);
  const filtered = excludeSecrets ? normalized.filter((item) => !secretPattern.test(item)) : normalized;
  return (filtered.length ? filtered : fallback).slice(0, 8);
}

function replaceAiPreviewList(id, items) {
  const list = document.getElementById(id);
  list.replaceChildren(...items.map((item) => {
    const node = document.createElement("li");
    node.textContent = item;
    return node;
  }));
}

function renderAiContextPreview(preview = {}) {
  const included = safeAiPreviewItems(
    preview.included_fields || preview.included || preview.sent_fields,
    ["当前经营范围", "计划聚合指标、数据时间和本地规则结论"],
    { excludeSecrets: true },
  );
  const excluded = safeAiPreviewItems(
    preview.excluded_fields || preview.excluded || preview.blocked_fields,
    ["Cookie、Token、API Key 和登录凭证", "消费者信息、订单明细、截图和网页全文"],
  );
  replaceAiPreviewList("ai-context-included", included);
  replaceAiPreviewList("ai-context-excluded", excluded);
  const time = String(preview.generated_at || preview.snapshot_at || "").slice(0, 19).replace("T", " ");
  const estimate = Number(preview.estimated_tokens || preview.token_estimate || 0);
  document.getElementById("ai-context-preview-meta").textContent = [
    preview.store_label || (selectedStoreKey ? "当前抖店（匿名范围）" : "等待打开抖店"),
    time ? `预览生成于 ${time}` : "本地脱敏策略已启用",
    estimate > 0 ? `预计 ${estimate} tokens` : "",
  ].filter(Boolean).join(" · ");
}

function renderAiProposals(payload = {}) {
  const items = Array.isArray(payload) ? payload : Array.isArray(payload.items) ? payload.items : Array.isArray(payload.proposals) ? payload.proposals : [];
  currentAiProposals = items.slice(0, 20);
  const list = document.getElementById("ai-proposal-list");
  if (!currentAiProposals.length) {
    const emptyState = document.createElement("div");
    emptyState.className = "ai-proposal-empty";
    emptyState.textContent = "暂无影子建议。先完成一次巡店，再运行影子分析。";
    list.replaceChildren(emptyState);
    return;
  }
  list.replaceChildren(...currentAiProposals.map((item, index) => {
    const card = document.createElement("article");
    const head = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = redactAiSecrets(String(item.title || item.plan_name || `影子建议 ${index + 1}`).slice(0, 120));
    const tag = document.createElement("span");
    const confidence = Number(item.confidence);
    tag.textContent = Number.isFinite(confidence) ? `置信度 ${Math.round(confidence * (confidence <= 1 ? 100 : 1))}%` : "待人工判断";
    head.append(title, tag);
    const suggestion = document.createElement("p");
    suggestion.textContent = redactAiSecrets(String(item.suggestion || item.rationale || item.summary || "AI 尚未提供可展示的判断依据。").slice(0, 500));
    const meta = document.createElement("small");
    meta.textContent = `仅供分析 · 未授权 · 不执行${item.created_at ? ` · ${String(item.created_at).slice(0, 19).replace("T", " ")}` : ""}`;
    card.append(head, suggestion, meta);
    return card;
  }));
}

async function loadAiStatus(generation = 0) {
  try {
    const status = await dashboardRead("/ai/status", generation);
    if (!dashboardLoadIsCurrent(generation)) return null;
    renderAiStatus(status);
    return status;
  } catch (error) {
    if (!dashboardLoadIsCurrent(generation)) return null;
    renderAiStatus({ configured: false, analysis_available: false });
    setAiMessage("ai-connection-message", error.message || "AI 状态暂时无法读取；本地巡店不受影响。", "warn");
    return null;
  }
}

async function loadAiContextPreview(generation = 0) {
  try {
    const preview = await dashboardRead("/ai/context-preview", generation);
    if (!dashboardLoadIsCurrent(generation)) return null;
    renderAiContextPreview(preview);
    return preview;
  } catch (error) {
    if (!dashboardLoadIsCurrent(generation)) return null;
    renderAiContextPreview({});
    document.getElementById("ai-context-preview-meta").textContent = `预览暂不可用：${error.message || "请先打开抖店并完成一次巡店"}`;
    return null;
  }
}

async function loadAiProposals(generation = 0) {
  try {
    const proposals = await bridgeFetch("/ai/proposals");
    if (!dashboardLoadIsCurrent(generation)) return null;
    renderAiProposals(proposals);
    return proposals;
  } catch (error) {
    if (!dashboardLoadIsCurrent(generation)) return null;
    renderAiProposals([]);
    setAiMessage("ai-shadow-message", error.message || "影子建议读取失败。", "error");
    return null;
  }
}

async function loadAiCenter(generation = 0) {
  await Promise.all([loadAiStatus(generation), loadAiContextPreview(generation)]);
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
    status.textContent = "等待首次巡店";
    note.textContent = memory.note || "打开抖店并完成一次巡店后，经营记忆会自动生效。";
    return empty(list, "尚未取得当前抖店数据，不会混用其他店铺的经验。");
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
    archive.dataset.taskWrite = "archive-memory";
    markAgentWriteControl(archive);
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

async function loadOperatorMemory(generation = 0) {
  try {
    const memory = await dashboardRead("/memory", generation);
    if (!dashboardLoadIsCurrent(generation)) return null;
    renderOperatorMemory(memory);
    return memory;
  } catch (error) {
    if (!dashboardLoadIsCurrent(generation)) return null;
    renderOperatorMemory({ note: error.message || "经营记忆暂时无法读取" });
    return null;
  }
}

async function loadDashboardOnce() {
  const loadGeneration = ++dashboardLoadGeneration;
  // Every refresh revalidates the authenticated bridge receipt. Keep writes
  // frozen during that short window; accepted connectivity is restored before
  // business renderers calculate their own disabled states.
  agentConnectionState = "checking";
  agentWriteBlockMessage = CHECKING_AGENT_WRITE_BLOCK_MESSAGE;
  registerAgentWriteControls();
  // Show loading skeleton
  showLoadingSkeleton();
  // Remember focus before re-render
  const focusedEl = document.activeElement;
  const focusId = focusedEl?.id || focusedEl?.closest("[id]")?.id;

  // Connection acceptance is a separate first phase. A failed Agent receipt
  // must be shown immediately instead of waiting for every business module to
  // exhaust its timeout and retry budget.
  let bridgeReceiptR;
  try {
    bridgeReceiptR = { status: "fulfilled", value: await dashboardBridgeReceiptRead(loadGeneration) };
  } catch (reason) {
    bridgeReceiptR = { status: "rejected", reason };
  }
  if (!dashboardLoadIsCurrent(loadGeneration)) return false;
  let bridgeReceipt = bridgeReceiptR.status === "fulfilled" ? bridgeReceiptR.value : {};
  const initialBackgroundFailure = dashboardBridgeReceiptFailure(bridgeReceiptR, bridgeReceipt);
  if (!dashboardBridgeAccepted(initialBackgroundFailure, bridgeReceipt)) {
    const failure = initialBackgroundFailure || dashboardBridgeFailure(
      bridgeReceipt,
      bridgeReceipt.error || "点击“修复本地 Agent”打开安装版恢复指引",
    );
    hideLoadingSkeleton();
    renderConnection(false, failure.title, failure.detail, failure);
    document.getElementById("headline").textContent = failure.headline;
    document.getElementById("summary").textContent = "写入与执行已冻结；重新加载或修复不会丢失本机数据。";
    renderJourneyCommand({ online: false });
    renderSimpleAgentError(failure.detail, failure);
    return true;
  }

  const [
    insightsR, actionCenterR, settingsR, opsR, extensionR, trendsR, accountsR, contextR, onboardingR, healthR, effectivenessR, readinessR, stopLossR, strategySimulationR, preflightR, shadowR, executionEffectivenessR, valueLedgerR, integrationsR, oceanengineR, oceanengineSyncR, oceanengineAccountCenterR, connectionGuideR, promotionReadinessR, a2PilotR, productionWriteR, controlTasksR, scheduleControlR, promotionPlanConsoleR, promotionOperationAuditR
  ] = await runDashboardReadPool([
    () => dashboardRead("/insights", loadGeneration),
    () => dashboardRead("/action-center", loadGeneration),
    () => dashboardRead("/settings", loadGeneration),
    () => dashboardRead("/ops-manager", loadGeneration),
    () => dashboardBackgroundRead(loadGeneration),
    () => dashboardRead("/trends?days=7", loadGeneration),
    () => dashboardRead("/qianchuan-accounts", loadGeneration),
    () => dashboardRead("/operation-context", loadGeneration),
    () => dashboardRead("/onboarding/status", loadGeneration),
    () => dashboardRead("/health-monitor", loadGeneration),
    () => dashboardRead("/effectiveness", loadGeneration),
    () => dashboardRead("/actions/readiness", loadGeneration),
    () => dashboardRead("/actions/stop-loss-queue", loadGeneration),
    () => dashboardRead("/actions/strategy-simulation", loadGeneration),
    () => dashboardRead("/actions/preflight", loadGeneration),
    () => dashboardRead("/actions/shadow", loadGeneration),
    () => dashboardRead("/actions/effectiveness", loadGeneration),
    () => dashboardRead("/value-ledger", loadGeneration),
    () => dashboardRead("/integrations", loadGeneration),
    () => dashboardRead("/oauth/oceanengine/status", loadGeneration),
    () => dashboardRead("/oauth/oceanengine/sync-status", loadGeneration),
    () => dashboardRead("/oauth/oceanengine/account-center", loadGeneration),
    () => dashboardRead("/connection-guide", loadGeneration),
    () => dashboardRead("/qianchuan/promotion-readiness", loadGeneration),
    () => dashboardRead("/chengfang/a2-pilot", loadGeneration),
    () => dashboardRead("/chengfang/production-write", loadGeneration),
    () => dashboardRead("/chengfang/control-tasks", loadGeneration),
    () => dashboardRead("/chengfang/schedule-control", loadGeneration),
    () => dashboardRead("/qianchuan/plan-console", loadGeneration),
    () => dashboardRead("/actions/audit?limit=500", loadGeneration),
  ]);

  // A newer refresh may finish before this request group. Never let an old
  // response restore a previous store/account, execution state or write gate.
  if (!dashboardLoadIsCurrent(loadGeneration)) return false;

  // The Agent can stop or lose its authenticated pairing while the business
  // pool is loading. Revalidate at the write-gate boundary so a stale positive
  // receipt can never reopen controls after the connection has changed.
  try {
    bridgeReceiptR = { status: "fulfilled", value: await dashboardBridgeReceiptRead(loadGeneration) };
  } catch (reason) {
    bridgeReceiptR = { status: "rejected", reason };
  }
  if (!dashboardLoadIsCurrent(loadGeneration)) return false;
  bridgeReceipt = bridgeReceiptR.status === "fulfilled" ? bridgeReceiptR.value : {};

  const failedModules = [
    ["经营建议", insightsR], ["动作中心", actionCenterR], ["经营设置", settingsR], ["今日任务", opsR],
    ["浏览器状态", extensionR], ["趋势", trendsR], ["店铺账户", accountsR], ["经营上下文", contextR],
    ["首次流程", onboardingR], ["数据健康", healthR], ["建议效果", effectivenessR], ["执行准备", readinessR],
    ["止损队列", stopLossR], ["策略模拟", strategySimulationR], ["执行前检查", preflightR], ["影子任务", shadowR],
    ["执行效果", executionEffectivenessR], ["价值账本", valueLedgerR], ["消息集成", integrationsR],
    ["官方授权", oceanengineR], ["官方同步", oceanengineSyncR], ["账户中心", oceanengineAccountCenterR],
    ["自动准备", connectionGuideR], ["投放准备度", promotionReadinessR], ["乘方试点", a2PilotR],
    ["乘方生产写入", productionWriteR],
    ["控制任务", controlTasksR], ["定时控制", scheduleControlR], ["投放计划", promotionPlanConsoleR],
    ["操作记录", promotionOperationAuditR],
  ].filter(([, result]) => result.status === "rejected").map(([label]) => label);
  if (oceanengineSyncR.status === "fulfilled" && oceanengineSyncR.value?.ok === false) {
    failedModules.push("官方同步");
  }
  currentDashboardFailures = failedModules;
  const loadWarning = document.getElementById("dashboard-load-warning");
  loadWarning.hidden = failedModules.length === 0;
  document.getElementById("dashboard-load-warning-detail").textContent = failedModules.length
    ? `读取失败：${failedModules.join("、")}。这些模块显示的是占位状态，不代表真实为 0。`
    : "全部模块已加载。";

  const val = (r, fallback) => r.status === "fulfilled" ? r.value : fallback;
  const insights = val(insightsR, { coverage: [], alerts: [], headline: "数据加载异常", summary: "部分模块连接失败，请稍后重试" });
  const actionCenter = val(actionCenterR, { plan_recommendations: [], inventory_alerts: [], product_graph: {}, shelf_analysis: {}, live_analysis: {}, creative_analysis: {} });
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
  const oceanengineAccountCenter = val(oceanengineAccountCenterR, {
    accounts: [], platform_write_enabled: false, automatic_batch_submit: false,
    notice: "账户中心暂时无法读取，请确认本地 Agent 已更新。",
  });
  const connectionGuide = val(connectionGuideR, {
    level: "L0", level_label: "连接状态读取失败", collapsed: false, levels: [],
    next_upgrade: { id: "start_store_scan", label: "打开抖店并开始", eta: "约 1 分钟", value: "完成后继续经营诊断", failure: "经营状态暂时无法读取，请打开抖店首页后重试。" },
    tutorial: [], operation_context: operationContext, onboarding,
  });
  const promotionReadiness = val(promotionReadinessR, null);
  const a2Pilot = val(a2PilotR, promotionReadiness?.autopilot_runtime?.a2_pilot || null);
  const productionWrite = val(productionWriteR, {
    status: { blockers: ["PRODUCTION_STATUS_UNAVAILABLE"] }, targets: [], active: null,
  });
  const controlTasks = val(controlTasksR, {
    families: [], tasks: [], importable_candidates: [], platform_write_enabled: false,
    notice: "控制任务中心暂时无法读取，请确认本地 Agent 已更新。",
  });
  const scheduleControl = val(scheduleControlR, {
    scope_bound: false, latest: null, active: null, next_events: [],
    platform_write_enabled: false, production_scheduler_enabled: false,
    notice: "定时启停暂时无法读取，请确认本地 Agent 已更新。",
  });
  const promotionPlanConsole = val(promotionPlanConsoleR, {
    rows: [], accounts: [], safe: true, platform_write_enabled: false, automatic_batch_submit: false,
    notice: "计划中心暂时无法读取，请确认本地 Agent 已更新。",
  });
  const promotionOperationAudit = val(promotionOperationAuditR, {
    actions: [], execution_enabled: false, summary: {},
    updated_at: "操作日志暂时无法读取",
  });
  const extensionDashboard = extensionResponse?.dashboard || {};
  const qianchuanScope = reconcileQianchuanScope(accounts, { available: accountsR.status === "fulfilled" });
  currentExtensionSettings = {
    autoSync: extensionDashboard.settings?.autoSync === true,
    intervalMinutes: Number(extensionDashboard.settings?.intervalMinutes || 5),
  };
  const backgroundFailure = dashboardBridgeReceiptFailure(bridgeReceiptR, bridgeReceipt);
  const bridgeStatus = bridgeReceipt;
  // Only the background's complete positive liveness/auth/version receipt may
  // open the global write gate. Missing fields remain fail-closed, while an
  // unrelated business-module read failure is shown as partial data instead
  // of being mislabeled as an offline Agent.
  const bridgeAcceptanceBlocked = !dashboardBridgeAccepted(backgroundFailure, bridgeStatus);

  // Hide loading skeleton
  hideLoadingSkeleton();

  // Protected reads can still succeed briefly while an old extension is being
  // replaced. The background's explicit auth/version verdict remains the
  // connection acceptance boundary and must freeze the workbench when false.
  if (bridgeAcceptanceBlocked) {
    const failure = backgroundFailure || dashboardBridgeFailure(
      bridgeStatus,
      bridgeStatus.error || "点击“修复本地 Agent”打开安装版恢复指引",
    );
    renderConnection(false, failure.title, failure.detail, failure);
    document.getElementById("headline").textContent = failure.headline;
    document.getElementById("summary").textContent = "写入与执行已冻结；重新加载或修复不会丢失本机数据。";
    renderJourneyCommand({
      online: false,
      onboarding: connectionGuide.onboarding || onboarding,
      connectionGuide,
      operationContext: connectionGuide.operation_context || operationContext,
      ops,
      scan: extensionDashboard.fullScan || {},
      capability: {},
      preflight,
      selectedAccountKey: "",
      planConsole: promotionPlanConsole,
      effectiveness: executionEffectiveness,
    });
    renderSimpleAgentError(failure.detail, failure);
    return true;
  }

  // Restore only the connection gate first. Business renderers below then set
  // their final eligibility-based disabled states without being overwritten by
  // stale offline snapshots.
  renderConnection(
    true,
    "本地 Agent 已连接",
    failedModules.length
      ? `连接与认证正常；${failedModules.length} 个数据模块暂未读取，已自动重试一次`
      : `已读取 ${insights.coverage?.length || 0} 类页面快照`,
  );

  // Reconcile the Agent-owned store/account catalog before any other module
  // derives readiness. Never let a previous browser selection fill a missing,
  // conflicting or failed catalog response.
  renderQianchuanAccounts(accounts, qianchuanScope);

  const capabilityReport = renderProductCapability({
    onboarding: connectionGuide.onboarding || onboarding,
    operation_context: connectionGuide.operation_context || operationContext,
    plan_console: promotionPlanConsole,
    automation_readiness: readiness,
    preflight,
    effectiveness: executionEffectiveness,
    selected_account_key: qianchuanScope.accountKey,
  });
  renderJourneyCommand({
    online: !bridgeAcceptanceBlocked,
    onboarding: connectionGuide.onboarding || onboarding,
    connectionGuide,
    operationContext: connectionGuide.operation_context || operationContext,
    ops,
    scan: extensionDashboard.fullScan || {},
    capability: capabilityReport || {},
    preflight,
    selectedAccountKey: qianchuanScope.accountKey,
    planConsole: promotionPlanConsole,
    effectiveness: executionEffectiveness,
  });

  document.getElementById("headline").textContent = insights.headline || "经营数据已同步";
  document.getElementById("summary").textContent = insights.summary || "请查看下方建议。";
  renderOperationContext(connectionGuide.operation_context || operationContext);
  renderOnboarding(connectionGuide.onboarding || onboarding);
  renderPlans(actionCenter.plan_recommendations || []);
  renderInventory(actionCenter.inventory_alerts || []);
  renderOperations(ops, actionCenter.product_graph || {}, actionCenter.shelf_analysis || {}, actionCenter.live_analysis || {}, actionCenter.creative_analysis || {}, insights.coverage || []);
  renderTodayFocus(ops);
  renderAlerts(insights.alerts || []);
  renderCoverage(insights.coverage || []);
  renderSettings(settings);
  renderIntegrations(integrations);
  renderOceanEngineOAuth(oceanengine);
  renderOceanEngineSync(oceanengineSync);
  renderOceanEngineAccountCenter(oceanengineAccountCenter);
  renderPromotionPlanConsole(promotionPlanConsole);
  renderPromotionOperationLog(promotionOperationAudit);
  renderConnectionGuide(connectionGuide, accounts);
  renderFullScan(extensionDashboard.fullScan || {});
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
  if (a2Pilot) currentChengfangA2Pilot = a2Pilot;
  if (typeof applyChengfangProductionWrite === "function") applyChengfangProductionWrite(productionWrite, { replace: true });
  currentControlTaskSummary = controlTasks;
  renderChengfangTrialConsole(currentChengfangAgentRuntime || {}, currentChengfangA2Pilot);
  renderAutopilotCenter();
  renderControlTaskCenter(controlTasks);
  renderScheduleControl(scheduleControl);
  if (currentChengfangAgentRuntime?.decision_automation?.running === true) {
    evaluateChengfangNow("page_sync").catch(() => undefined);
  }
  await Promise.all([
    loadSystemStatus(loadGeneration),
    loadAiCenter(loadGeneration),
    loadOperatorMemory(loadGeneration),
  ]);
  if (!dashboardLoadIsCurrent(loadGeneration)) return false;

  // Restore focus if the focused element still exists
  if (focusId) {
    const restored = document.getElementById(focusId);
    if (restored) restored.focus();
  }

  latestBrief = [
    insights.headline, insights.summary,
    ...(ops.today_top_actions || []).slice(0, 8).map((item, index) => `今日任务 ${index + 1}. [${item.owner}] ${item.title}：${item.action}`),
    ...(actionCenter.plan_recommendations || []).slice(0, 5).map((item, index) => `千川 ${index + 1}. ${item.plan}：${item.suggestion}`),
    ...(actionCenter.creative_analysis?.recommendations || []).slice(0, 5).map((item, index) => `素材 ${index + 1}. ${item.title}：${item.action}`),
    ...(actionCenter.inventory_alerts || []).slice(0, 5).map((item, index) => `库存 ${index + 1}. ${item.product}：${item.suggestion}`),
    ...(shadow.items || []).slice(0, 5).map((item, index) => `影子执行 ${index + 1}. ${item.plan_name}：${item.status_label}`),
  ].filter(Boolean).join("\n");
  await maybeReturnFromTargetedScan(extensionDashboard.fullScan || {}, loadGeneration);
  return true;
}

function startDashboardLoad() {
  const operation = loadDashboardOnce();
  dashboardLoadInFlight = operation;
  operation.then(
    () => {
      if (dashboardLoadInFlight === operation) dashboardLoadInFlight = null;
    },
    () => {
      if (dashboardLoadInFlight === operation) dashboardLoadInFlight = null;
    },
  );
  return operation;
}

function loadDashboard() {
  if (!dashboardLoadInFlight) return startDashboardLoad();
  if (!dashboardLoadTrailing) {
    const active = dashboardLoadInFlight;
    dashboardLoadTrailing = active.catch(() => undefined).then(() => {
      dashboardLoadTrailing = null;
      return startDashboardLoad();
    });
  }
  return dashboardLoadTrailing;
}

function maybeRecoverAgentOnRuntimeReady() {
  if (agentConnectionState !== "offline") return Promise.resolve(false);
  if (agentAutoRecoveryInFlight) return agentAutoRecoveryInFlight;
  const now = Date.now();
  if (now - agentAutoRecoveryLastAttemptAt < AGENT_AUTO_RECOVERY_COOLDOWN_MS) {
    return Promise.resolve(false);
  }
  agentAutoRecoveryLastAttemptAt = now;
  const operation = (async () => {
    try {
      const receipt = await dashboardRuntimeMessage(
        { type: "test-bridge" },
        "自动复核本地 Agent 连接",
      );
      if (dashboardBridgeReceiptNeedsConfirmation(receipt)) return false;
      await refreshAll(false);
      return true;
    } catch (_) {
      return false;
    }
  })();
  const trackedOperation = operation.finally(() => {
    if (agentAutoRecoveryInFlight === trackedOperation) agentAutoRecoveryInFlight = null;
  });
  agentAutoRecoveryInFlight = trackedOperation;
  return agentAutoRecoveryInFlight;
}

globalThis.addEventListener?.("dian-agent-runtime-ready", () => {
  maybeRecoverAgentOnRuntimeReady();
});
globalThis.addEventListener?.("focus", () => {
  maybeRecoverAgentOnRuntimeReady();
});

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

function renderDashboardLoadFailure(error = {}) {
  // Rendering, manual sync or one dashboard module can fail after the Agent
  // has already passed an independent connection receipt. Do not overwrite
  // that truth with “Agent not started”. Freeze writes until a clean refresh
  // and present a retryable workbench error.
  hideLoadingSkeleton();
  if (agentConnectionState !== "offline") {
    agentConnectionState = "checking";
    agentWriteBlockMessage = CHECKING_AGENT_WRITE_BLOCK_MESSAGE;
    registerAgentWriteControls();
  }
  const warning = document.getElementById("dashboard-load-warning");
  if (warning) warning.hidden = false;
  const warningDetail = document.getElementById("dashboard-load-warning-detail");
  if (warningDetail) {
    warningDetail.textContent = `工作台未完整加载：${error.message || "未知界面错误"}。请点击“只需重试加载”。`;
  }
  document.getElementById("headline").textContent = "工作台加载异常";
  document.getElementById("summary").textContent = "Agent 状态未被误判；写入与执行暂时冻结，重新加载不会丢失本机数据。";
}

async function refreshAll(syncFirst = false) {
  const button = document.getElementById("sync-diagnose");
  try {
    if (syncFirst) {
      button.disabled = true;
      button.textContent = "正在同步…";
      await dashboardRuntimeMessage({ type: "manual-sync" }, "启动手动同步");
      await new Promise((resolve) => setTimeout(resolve, 800));
    }
    await loadDashboard();
  } catch (error) {
    renderDashboardLoadFailure(error);
  } finally {
    button.disabled = false;
    button.textContent = "刷新当前建议";
  }
}

function qianchuanSyncAgeLabel(timestamp, now = Date.now()) {
  const value = Number(timestamp || 0);
  if (!Number.isFinite(value) || value <= 0) return "尚未同步";
  const elapsed = Math.max(0, Number(now || Date.now()) - value);
  if (elapsed < 60_000) return "刚刚";
  if (elapsed < 60 * 60_000) return `${Math.max(1, Math.floor(elapsed / 60_000))} 分钟前`;
  const date = new Date(value);
  const current = new Date(now);
  if (date.toDateString() === current.toDateString()) {
    return `今天 ${date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
  }
  return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}

function qianchuanSyncIsFresh(timestamp, now = Date.now()) {
  const value = Number(timestamp || 0);
  return Number.isFinite(value) && value > 0 && Math.max(0, Number(now || Date.now()) - value) <= QIANCHUAN_SYNC_FRESH_MS;
}

function setQianchuanSyncDockHidden(hidden, { persist = false } = {}) {
  const isHidden = hidden === true;
  const dock = document.getElementById("qianchuan-sync-dock");
  const restoreButton = document.getElementById("qianchuan-sync-dock-restore");
  if (dock) dock.hidden = isHidden;
  document.body.classList.toggle("qianchuan-dock-visible", !isHidden);
  if (restoreButton) {
    restoreButton.hidden = !isHidden;
    restoreButton.textContent = isHidden ? "显示千川悬浮同步（已隐藏）" : "显示千川悬浮同步";
  }
  if (!persist) return Promise.resolve();
  return chrome.storage.local.set({ [QIANCHUAN_SYNC_DOCK_HIDDEN_KEY]: isHidden });
}

function setQianchuanSyncDockCompact(compact, { persist = false } = {}) {
  const isCompact = compact === true;
  const dock = document.getElementById("qianchuan-sync-dock");
  const toggle = document.getElementById("qianchuan-sync-dock-collapse");
  if (dock) dock.classList.toggle("compact", isCompact);
  if (toggle) {
    toggle.textContent = isCompact ? "+" : "−";
    toggle.setAttribute("aria-expanded", String(!isCompact));
    toggle.setAttribute("aria-label", isCompact ? "展开千川快速同步悬浮窗" : "收起千川快速同步悬浮窗");
    toggle.title = isCompact ? "展开悬浮窗" : "收起悬浮窗";
  }
  if (!persist) return Promise.resolve();
  qianchuanSyncDockCompactPreferenceSet = true;
  return chrome.storage.local.set({ [QIANCHUAN_SYNC_DOCK_COMPACT_KEY]: isCompact });
}

function setQianchuanSyncUi(state = "idle", detail = "读取最近页面") {
  const dock = document.getElementById("qianchuan-sync-dock");
  const dockButton = document.getElementById("qianchuan-sync-dock-button");
  const mainButton = document.getElementById("current-qianchuan-button");
  const dockLabel = document.getElementById("qianchuan-sync-dock-label");
  const dockStatus = document.getElementById("qianchuan-sync-dock-status");
  const running = state === "syncing";
  const normalizedState = ["idle", "syncing", "success", "error", "stale"].includes(state) ? state : "idle";
  dock.classList.remove("idle", "syncing", "success", "error", "stale");
  dock.classList.add(normalizedState);
  dock.setAttribute("aria-busy", String(running));
  dockButton.disabled = running;
  mainButton.disabled = running;
  dockLabel.textContent = running ? "同步中" : normalizedState === "success" ? "已同步" : normalizedState === "stale" ? "需更新" : normalizedState === "error" ? "同步失败" : "同步千川";
  dockStatus.textContent = detail;
  dockButton.title = detail;
  dockButton.setAttribute("aria-label", running ? `正在同步千川：${detail}` : `同步最近访问的千川页面。当前状态：${detail}`);
  mainButton.textContent = running ? "正在同步千川页面…" : "同步最近千川页面";
}

function restoreQianchuanSyncUi(successRecord = {}, attemptRecord = {}) {
  if (qianchuanSyncFreshnessTimer) clearTimeout(qianchuanSyncFreshnessTimer);
  qianchuanSyncFreshnessTimer = null;
  const success = successRecord?.status === "success" ? successRecord : {};
  const attempt = attemptRecord && typeof attemptRecord === "object" ? attemptRecord : {};
  latestQianchuanSyncUiReceipt = { success, attempt };
  if (attempt.status === "error" && Number(attempt.timestamp || 0) > Number(success.timestamp || 0)) {
    const previous = success.timestamp ? `；上次成功 ${qianchuanSyncAgeLabel(success.timestamp)}` : "";
    setQianchuanSyncUi("error", `${attempt.message || "请先打开千川"}${previous}`);
  } else if (success.status === "success") {
    const label = success.account_label || LABELS[success.page_type] || "千川页面";
    const state = qianchuanSyncIsFresh(success.timestamp) ? "success" : "stale";
    setQianchuanSyncUi(state, `${label} · ${qianchuanSyncAgeLabel(success.timestamp)}`);
    if (state === "success") {
      const remaining = Math.max(1_000, QIANCHUAN_SYNC_FRESH_MS - (Date.now() - Number(success.timestamp || 0)) + 1_000);
      qianchuanSyncFreshnessTimer = setTimeout(() => restoreQianchuanSyncUi(success, attempt), remaining);
    }
  } else {
    setQianchuanSyncUi("idle", "读取最近页面");
  }
}

function mergeQianchuanSyncReceipts(manualSuccess = {}, manualAttempt = {}, sourceReceipt = {}) {
  const receiptSuccess = Number(sourceReceipt.last_success_at || 0) > 0 ? {
    status: "success",
    timestamp: Number(sourceReceipt.last_success_at),
    account_label: String(sourceReceipt.account_label || ""),
    account_key: String(sourceReceipt.account_key || ""),
    store_key: String(sourceReceipt.store_key || ""),
    page_type: String(sourceReceipt.page_type || ""),
  } : {};
  const success = Number(receiptSuccess.timestamp || 0) >= Number(manualSuccess.timestamp || 0)
    ? receiptSuccess
    : manualSuccess;
  const receiptAttempt = Number(sourceReceipt.last_attempt_at || 0) > 0 ? {
    ...(sourceReceipt.status === "success" ? receiptSuccess : {}),
    status: sourceReceipt.status === "success" ? "success" : "error",
    timestamp: Number(sourceReceipt.last_attempt_at),
    message: String(sourceReceipt.message || ""),
    error_code: String(sourceReceipt.error_code || ""),
  } : {};
  const attempt = Number(receiptAttempt.timestamp || 0) >= Number(manualAttempt.timestamp || 0)
    ? receiptAttempt
    : manualAttempt;
  return { success, attempt };
}

function normalizeQianchuanSyncOptions(options = {}) {
  return {
    expectedPageTypes: [...new Set((Array.isArray(options.expectedPageTypes) ? options.expectedPageTypes : [])
      .map((item) => String(item || "").trim().toLowerCase()).filter(Boolean))].sort(),
    expectedAccountKey: String(options.expectedAccountKey || selectedQianchuanAccount || "").trim().toLowerCase(),
    expectedStoreKey: String(options.expectedStoreKey || selectedStoreKey || "").trim().toLowerCase(),
    purpose: String(options.purpose || "当前千川页面同步").trim(),
  };
}

function qianchuanSyncContractKey(options = {}) {
  const contract = normalizeQianchuanSyncOptions(options);
  return JSON.stringify({
    store_key: contract.expectedStoreKey,
    account_key: contract.expectedAccountKey,
    page_types: contract.expectedPageTypes,
    purpose: contract.purpose.toLowerCase(),
  });
}

function qianchuanSyncResultScope(result = {}) {
  return {
    storeKey: String(result?.store?.key || result?.store_key || "").trim().toLowerCase(),
    accountKey: String(result?.account?.key || result?.account_key || "").trim().toLowerCase(),
  };
}

function assertQianchuanSyncResult(result = {}, options = {}) {
  const contract = normalizeQianchuanSyncOptions(options);
  const resultPageType = String(result.page_type || "").trim().toLowerCase();
  if (contract.expectedPageTypes.length && !contract.expectedPageTypes.includes(resultPageType)) {
    const expected = contract.expectedPageTypes.map((item) => LABELS[item] || item).join("、");
    const actual = LABELS[result.page_type] || result.page_type || "未识别页面";
    throw new Error(`${contract.purpose}需要打开${expected}，当前读取到${actual}；本次未保存。`);
  }
  const resultScope = qianchuanSyncResultScope(result);
  if (contract.expectedStoreKey && resultScope.storeKey !== contract.expectedStoreKey) {
    const error = new Error(`${contract.purpose}返回的店铺范围与当前店铺不一致；结果未用于本次流程。`);
    error.code = resultScope.storeKey ? "SYNC_RESULT_STORE_MISMATCH" : "SYNC_RESULT_STORE_MISSING";
    throw error;
  }
  if (contract.expectedAccountKey && resultScope.accountKey !== contract.expectedAccountKey) {
    const error = new Error(`${contract.purpose}返回的千川账户与当前锁定账户不一致；结果未用于本次流程。`);
    error.code = resultScope.accountKey ? "SYNC_RESULT_ACCOUNT_MISMATCH" : "SYNC_RESULT_ACCOUNT_MISSING";
    throw error;
  }
  return result;
}

async function syncRecentQianchuanPage(options = {}) {
  const contract = normalizeQianchuanSyncOptions(options);
  const contractKey = qianchuanSyncContractKey(contract);
  const existing = qianchuanSyncPromises.get(contractKey);
  if (existing) return existing;
  const syncGeneration = ++qianchuanSyncGeneration;
  qianchuanSyncActiveCount += 1;
  let requestPromise;
  requestPromise = (async () => {
    const hint = document.getElementById("qianchuan-account-hint");
    setQianchuanSyncUi("syncing", qianchuanSyncActiveCount > 1 ? `正在处理 ${qianchuanSyncActiveCount} 项同步` : contract.purpose);
    try {
      const response = await chrome.runtime.sendMessage({
        type: "sync-current-qianchuan",
        expected_page_types: contract.expectedPageTypes,
        expected_account_key: contract.expectedAccountKey,
        expected_store_key: contract.expectedStoreKey,
        purpose: contract.purpose,
      });
      if (!response?.ok) {
        const syncError = new Error(response?.error || "同步失败");
        syncError.code = String(response?.code || response?.error_code || "");
        throw syncError;
      }
      const result = assertQianchuanSyncResult(response.result || {}, contract);
      const accountLabel = result.account?.label || "";
      const pageLabel = LABELS[result.page_type] || result.page_type || "千川页面";
      const timestamp = Date.now();
      const record = {
        status: "success",
        timestamp,
        account_label: accountLabel,
        account_key: result.account?.key || "",
        store_key: result.store?.key || "",
        page_type: result.page_type || "",
        tab_id: result.tab?.id || null,
      };
      qianchuanFeatureDeferred = false;
      let localStateWarning = "";
      try {
        await chrome.storage.local.set({ qianchuanFeatureDeferred: false });
      } catch (storageError) {
        localStateWarning = storageError?.message || "本机状态保存失败";
      }
      if (syncGeneration === qianchuanSyncGeneration) {
        latestQianchuanSyncUiReceipt = { success: record, attempt: record };
        try {
          await chrome.storage.local.set({ lastQianchuanManualSync: record, [QIANCHUAN_SYNC_ATTEMPT_KEY]: record });
        } catch (storageError) {
          localStateWarning = storageError?.message || localStateWarning || "同步回执保存失败";
        }
      }
      const itemCount = Number(result.quality?.row_count ?? result.snapshot?.quality?.row_count);
      const countLabel = Number.isFinite(itemCount) ? ` · ${itemCount} 条` : "";
      if (syncGeneration === qianchuanSyncGeneration) {
        hint.textContent = `已核对并同步${pageLabel}${accountLabel ? ` · ${accountLabel}` : ""}${countLabel} · ${new Date(timestamp).toLocaleString()}。${localStateWarning ? ` 本机回执未保存：${localStateWarning}` : ""}`;
        restoreQianchuanSyncUi(record, record);
        try {
          await loadDashboard();
        } catch (refreshError) {
          hint.textContent = `数据已同步并保存，但工作台刷新失败：${refreshError?.message || "请点击刷新重试"}`;
          renderDashboardLoadFailure(refreshError);
        }
      }
      return result;
    } catch (error) {
      const message = error.message || "同步失败，请先打开巨量千川页面";
      if (syncGeneration === qianchuanSyncGeneration) {
        const failure = { status: "error", timestamp: Date.now(), message, error_code: String(error?.code || "") };
        latestQianchuanSyncUiReceipt = { success: latestQianchuanSyncUiReceipt.success, attempt: failure };
        let receiptWarning = "";
        try {
          await chrome.storage.local.set({ [QIANCHUAN_SYNC_ATTEMPT_KEY]: failure });
        } catch (storageError) {
          receiptWarning = storageError?.message || "失败回执未保存";
        }
        hint.textContent = receiptWarning ? `${message}；本机回执未保存：${receiptWarning}` : message;
        setQianchuanSyncUi("error", message);
      }
      throw error;
    } finally {
      qianchuanSyncActiveCount = Math.max(0, qianchuanSyncActiveCount - 1);
      if (qianchuanSyncPromises.get(contractKey) === requestPromise) {
        qianchuanSyncPromises.delete(contractKey);
      }
      if (qianchuanSyncActiveCount > 0) {
        setQianchuanSyncUi("syncing", `仍有 ${qianchuanSyncActiveCount} 项同步进行中`);
      } else if (syncGeneration !== qianchuanSyncGeneration) {
        restoreQianchuanSyncUi(latestQianchuanSyncUiReceipt.success || {}, latestQianchuanSyncUiReceipt.attempt || {});
      }
    }
  })();
  qianchuanSyncPromises.set(contractKey, requestPromise);
  return requestPromise;
}

document.addEventListener("DOMContentLoaded", async () => {
  registerAgentWriteControls();
  const entryRoute = requestedWorkbenchEntryRoute();
  const stored = await chrome.storage.local.get(["preferredRole", "workbenchScene", "templateChecks", "scanStorePreference", "scanAccountPreference", "lastQianchuanManualSync", QIANCHUAN_SYNC_ATTEMPT_KEY, PAGE_SYNC_RECEIPTS_KEY, "qianchuanFeatureDeferred", CHENGFANG_LOCAL_PLAN_KEY, CHENGFANG_TRIAL_CONFIG_KEY, AUTOPILOT_CENTER_SETTINGS_KEY, AUTOPILOT_PACKAGES_KEY, AUTOPILOT_ACTIVITY_KEY, MATERIAL_GOVERNANCE_PACKAGES_KEY, PROMOTION_PLAN_FILTERS_KEY, PROMOTION_PLAN_BINDINGS_KEY, PROMOTION_BULK_ACTION_DRAFTS_KEY, PROMOTION_BATCH_DRAFTS_KEY, EXPERIENCE_MODE_KEY, JOURNEY_LANE_KEY, QIANCHUAN_SYNC_DOCK_HIDDEN_KEY, QIANCHUAN_SYNC_DOCK_COMPACT_KEY]);
  if (entryRoute === CHENGFANG_DEMO_ENTRY_ROUTE) currentRole = "直播投放";
  else if (stored.preferredRole) currentRole = ROLE_MIGRATION[stored.preferredRole] || stored.preferredRole;
  if (!ROLE_WORKBENCH[currentRole]) currentRole = "货架商品";
  experienceMode = entryRoute === CHENGFANG_DEMO_ENTRY_ROUTE
    ? "professional"
    : globalThis.DianSimpleExperience.normalizeMode(stored[EXPERIENCE_MODE_KEY]);
  applyExperienceMode(experienceMode, { persist: false });
  qianchuanSyncDockCompactPreferenceSet = Object.hasOwn(stored, QIANCHUAN_SYNC_DOCK_COMPACT_KEY);
  setQianchuanSyncDockCompact(qianchuanSyncDockCompactPreferenceSet
    ? stored[QIANCHUAN_SYNC_DOCK_COMPACT_KEY] === true
    : experienceMode === "simple", { persist: false });
  setQianchuanSyncDockHidden(stored[QIANCHUAN_SYNC_DOCK_HIDDEN_KEY] === true);
  if (SCENE_WORKBENCH[stored.workbenchScene]) workbenchScene = stored.workbenchScene;
  // The Agent catalog is the only authority for the current operating scope.
  // Browser preferences must never briefly revive a previous store/account
  // before the authenticated dashboard response arrives.
  selectedQianchuanAccount = "";
  selectedStoreKey = "";
  currentJourneyLane = entryRoute === CHENGFANG_DEMO_ENTRY_ROUTE
    ? "ads"
    : ["store", "ads"].includes(stored[JOURNEY_LANE_KEY]) ? stored[JOURNEY_LANE_KEY] : "";
  qianchuanFeatureDeferred = Boolean(stored.qianchuanFeatureDeferred);
  if (stored.templateChecks && typeof stored.templateChecks === "object") {
    // The browser preference is only a hint until the Agent returns its
    // selected store. Do not delete another store's checklist during this
    // startup window; the first explicit checklist change performs date-only
    // pruning while preserving every store for today.
    templateChecks = { ...stored.templateChecks };
  }
  document.querySelectorAll("#role-nav button").forEach((item) => item.classList.toggle("active", item.dataset.role === currentRole));
  await chrome.storage.local.set({ preferredRole: currentRole });
  const qianchuanReceipt = mergeQianchuanSyncReceipts(
    stored.lastQianchuanManualSync || {},
    stored[QIANCHUAN_SYNC_ATTEMPT_KEY] || {},
    stored[PAGE_SYNC_RECEIPTS_KEY]?.qianchuan || {},
  );
  restoreQianchuanSyncUi(qianchuanReceipt.success, qianchuanReceipt.attempt);
  currentChengfangTrialConfig = globalThis.DianChengfangTrialPolicy.normalizeConfig(stored[CHENGFANG_TRIAL_CONFIG_KEY] || {});
  currentAutopilotCenterSettings = globalThis.DianAutopilotCenter.normalizeSettings(stored[AUTOPILOT_CENTER_SETTINGS_KEY] || {});
  currentAutopilotPackages = stored[AUTOPILOT_PACKAGES_KEY] && typeof stored[AUTOPILOT_PACKAGES_KEY] === "object" ? stored[AUTOPILOT_PACKAGES_KEY] : {};
  currentAutopilotActivity = Array.isArray(stored[AUTOPILOT_ACTIVITY_KEY]) ? stored[AUTOPILOT_ACTIVITY_KEY].slice(0, 60) : [];
  currentMaterialGovernancePackages = stored[MATERIAL_GOVERNANCE_PACKAGES_KEY] && typeof stored[MATERIAL_GOVERNANCE_PACKAGES_KEY] === "object" ? stored[MATERIAL_GOVERNANCE_PACKAGES_KEY] : {};
  currentPromotionPlanFilters = globalThis.DianPromotionPlanCenter.normalizeFilters(stored[PROMOTION_PLAN_FILTERS_KEY] || {});
  currentPromotionPlanBindings = stored[PROMOTION_PLAN_BINDINGS_KEY] && typeof stored[PROMOTION_PLAN_BINDINGS_KEY] === "object" ? stored[PROMOTION_PLAN_BINDINGS_KEY] : {};
  allPromotionBulkActionDrafts = Array.isArray(stored[PROMOTION_BULK_ACTION_DRAFTS_KEY]) ? stored[PROMOTION_BULK_ACTION_DRAFTS_KEY].slice(0, 20) : [];
  currentPromotionBulkActionDrafts = globalThis.DianPromotionBulkActions.filterDraftsForScope(allPromotionBulkActionDrafts, currentPromotionBulkScope()).slice(0, 20);
  currentPromotionBulkActionDraft = currentPromotionBulkActionDrafts[0] || null;
  currentPromotionBatchDrafts = Array.isArray(stored[PROMOTION_BATCH_DRAFTS_KEY]) ? stored[PROMOTION_BATCH_DRAFTS_KEY].slice(0, 20) : [];
  currentPromotionBatchDraft = currentPromotionBatchDrafts[0] || null;
  currentChengfangLocalPlanStore = normalizeChengfangLocalPlanStore(stored[CHENGFANG_LOCAL_PLAN_KEY] || {});
  // Browser preferences are only hints until the Agent catalog reconciles the
  // store/account pair. Keep the planner empty and do not POST during startup.
  restoreChengfangLocalPlan({}, { scope: chengfangPlanScope("", "") });
  renderWorkbench();
  renderPromotionBatchDraft();
  renderPromotionBulkActionDraft();
  renderPromotionBulkSelection();
  syncPromotionBulkActionControls();
  applyModuleVisibility();
  if (entryRoute === CHENGFANG_DEMO_ENTRY_ROUTE) setPromotionView("chengfang");
  await reportExtensionInstallSource().catch(() => undefined);
  refreshAll(false).finally(() => focusWorkbenchEntryRoute(entryRoute));
});
document.getElementById("chengfang-candidate-path-action").addEventListener("click", async (event) => {
  const action = currentChengfangCandidatePath?.next_action || { id: event.currentTarget.dataset.action };
  if (action.id === "sync_chengfang") {
    document.getElementById("chengfang-sync").click();
    return;
  }
  if (action.id === "shadow") {
    document.getElementById("chengfang-shadow-toggle").click();
    return;
  }
  if (action.id === "evaluate") {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "正在运行首次评估…";
    try {
      await evaluateChengfangNow("manual");
    } catch (error) {
      document.getElementById("chengfang-candidate-path-next").textContent = error?.message || "首次评估失败，请检查当前证据。";
    } finally {
      renderChengfangCandidatePath();
    }
    return;
  }
  revealChengfangCandidatePathTarget(action);
});
document.querySelectorAll('input[name="chengfang-goal"]').forEach((input) => input.addEventListener("change", async () => {
  document.getElementById("chengfang-goal-status").textContent = `已选择“${globalThis.DianChengfangPlanner.GOALS[input.value]}”；偏好仅保存在本机。`;
  await saveChengfangLocalPlan();
}));
document.querySelectorAll("[data-chengfang-input]").forEach((input) => input.addEventListener("input", () => saveChengfangLocalPlan()));
document.querySelectorAll("[data-chengfang-evidence]").forEach((input) => input.addEventListener("input", () => saveChengfangLocalPlan()));
document.querySelectorAll("[data-chengfang-boundary]").forEach((input) => input.addEventListener("input", () => saveChengfangLocalPlan()));
document.getElementById("chengfang-boundary-draft").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await applyChengfangBoundaryDraft();
  } catch (error) {
    setChengfangAssistStatus("chengfang-boundary-draft-status", error?.message || "保守草案生成失败，请重新加载扩展。", "danger");
  } finally {
    button.disabled = false;
  }
});
document.getElementById("chengfang-evidence-hydrate").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await hydrateChengfangEvidence(button);
  } catch (error) {
    setChengfangAssistStatus("chengfang-evidence-hydrate-status", error?.message || "同步证据补齐失败，请检查本地 Agent。", "danger");
  } finally {
    button.disabled = false;
  }
});
document.getElementById("chengfang-profile-reset").addEventListener("click", async () => {
  if (!confirm("确认重置本机乘方经营建档和影子观察记录？此操作不会影响千川平台数据。")) return;
  const profileScope = currentChengfangProfileScope();
  const resetRequest = chengfangScopedPostPayload({ confirm: true }, profileScope);
  const packageScopeKey = autopilotScopeKey();
  const nextPackages = { ...currentAutopilotPackages };
  delete nextPackages[packageScopeKey];
  currentAutopilotPackages = nextPackages;
  currentChengfangLocalPlanStore = removeChengfangPlanForScope(currentChengfangLocalPlanStore, profileScope);
  await chrome.storage.local.remove([CHENGFANG_TRIAL_CONFIG_KEY]);
  await chrome.storage.local.set({
    [CHENGFANG_LOCAL_PLAN_KEY]: currentChengfangLocalPlanStore,
    [AUTOPILOT_PACKAGES_KEY]: currentAutopilotPackages,
  });
  restoreChengfangLocalPlan({}, { scope: profileScope });
  await bridgeFetch("/chengfang/autopilot/reset", { method: "POST", body: JSON.stringify(resetRequest.body) })
    .then((result) => validateChengfangScopedResponse(profileScope, result))
    .catch(() => undefined);
  currentChengfangTrialConfig = globalThis.DianChengfangTrialPolicy.normalizeConfig({});
  currentChengfangA2Pilot = null;
  currentChengfangDemoFixture = null;
  document.querySelectorAll('input[name="chengfang-goal"]').forEach((input) => { input.checked = false; });
  document.querySelectorAll("[data-chengfang-input], [data-chengfang-evidence], [data-chengfang-boundary]").forEach((input) => { input.value = ""; input.classList.remove("invalid"); });
  setChengfangAssistStatus("chengfang-boundary-draft-status", "只建议单次 5%、每日 1 次等技术护栏；预算、利润、退款和库存红线仍由你确认。");
  setChengfangAssistStatus("chengfang-evidence-hydrate-status", "优先读取当前店铺与千川账户的最新快照；不会猜测利润、库存、履约或已实现亏损。");
  restoreChengfangLocalPlan({}, { scope: profileScope });
  await recordAutopilotActivity("config", "重置当前店铺策略包", "已清除本机经营建档和策略包；千川平台数据未修改。");
});
document.getElementById("chengfang-trial-environment").addEventListener("change", async (event) => {
  const nextEnvironment = event.currentTarget.value === "production" ? "production" : "demo";
  const previous = currentChengfangTrialConfig.environment;
  const view = chengfangTrialView();
  if (nextEnvironment === "production" && view.master_enabled) {
    if (!confirm("切换到生产只读环境会立即停止 A2 模拟试运行。官方调控能力已发现，但当前应用权限、账户白名单与生产适配器尚未验收，生产环境仍不能执行投放。确认继续？")) {
      event.currentTarget.value = previous;
      return;
    }
    try {
      const result = await bridgeFetch("/chengfang/a2-pilot/stop", {
        method: "POST",
        body: JSON.stringify({ confirm: true, reason: "ENVIRONMENT_CHANGED_TO_PRODUCTION_READ_ONLY" }),
      });
      applyChengfangA2Response(result);
    } catch (error) {
      event.currentTarget.value = previous;
      setChengfangTrialError(error, "停止 A2 失败，未切换运行环境。");
      return;
    }
  }
  if (nextEnvironment === "production") currentChengfangDemoFixture = null;
  await saveChengfangTrialConfig({ ...currentChengfangTrialConfig, environment: nextEnvironment });
});
document.getElementById("chengfang-production-refresh").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在读取官方快照…";
  try {
    await refreshChengfangProductionWrite();
  } finally {
    button.disabled = false;
    button.textContent = "刷新可写计划";
  }
});
document.getElementById("chengfang-production-next").addEventListener("click", async (event) => {
  const action = String(event.currentTarget.dataset.action || "refresh");
  if (action === "reconcile") {
    document.getElementById("chengfang-production-reconcile")?.click();
    return;
  }
  if (action === "resume") {
    document.getElementById("chengfang-production-resume")?.click();
    return;
  }
  if (action === "stop") {
    document.getElementById("chengfang-production-stop")?.click();
    return;
  }
  if (action === "version") {
    navigateToWorkspaceElement("data-version-center", { title: "版本与更新", highlight: true });
    return;
  }
  if (action === "store" || action === "account") {
    const guide = document.getElementById("connection-guide");
    if (guide) {
      guide.hidden = false;
      guide.setAttribute("aria-hidden", "false");
      guide.className = "connection-guide expanded";
      document.getElementById("connection-guide-expanded").hidden = false;
      if (action === "store") document.getElementById("connection-store-controls").hidden = false;
      const focusElement = action === "account"
        ? document.getElementById("linked-account-select") || document.getElementById("current-qianchuan-button")
        : document.getElementById("qianchuan-account-select") || document.getElementById("connection-guide-action");
      navigateToWorkspaceElement(guide, {
        title: action === "store" ? "确认当前店铺" : "选择千川账户",
        focusElement,
        highlight: true,
      });
    }
    return;
  }
  if (action === "oauth" || action === "sync") {
    const oauth = document.getElementById("oceanengine-oauth-card");
    if (oauth) oauth.open = true;
    navigateToWorkspaceElement(oauth, {
      title: action === "oauth" ? "官方 API 授权" : "同步当前账户计划",
      focusElement: document.getElementById(action === "oauth" ? "authorize-oceanengine" : "sync-oceanengine-data"),
      highlight: true,
    });
    return;
  }
  if (action === "operation") {
    const target = document.getElementById("chengfang-production-authorization");
    target?.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  if (action === "select") {
    document.getElementById("chengfang-production-target")?.focus();
    document.getElementById("chengfang-production-target")?.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await refreshChengfangProductionWrite();
  } finally {
    button.disabled = false;
  }
});
document.getElementById("chengfang-production-stop").addEventListener("click", async (event) => {
  if (!confirm("确认立即停止乘方生产写入？尚未执行的预检与授权会取消；已发出的请求不会被伪装成可撤回。")) return;
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在开启生产紧停…";
  try {
    const result = await bridgeFetch("/chengfang/production-write/stop", {
      method: "POST",
      body: JSON.stringify({ confirm: true, reason: "USER_REQUESTED_FROM_EXTENSION" }),
    });
    applyChengfangProductionWrite(result);
  } catch (error) {
    setChengfangProductionError(error, "生产紧急停止失败，请检查本地 Agent；不要继续执行写入。");
  } finally {
    button.textContent = "紧急停止生产";
    renderChengfangProductionWrite();
  }
});
document.getElementById("chengfang-production-resume").addEventListener("click", async (event) => {
  const phrase = String(currentChengfangProductionWrite.kill_switch?.resume_confirmation_phrase || "");
  if (!phrase) {
    await refreshChengfangProductionWrite();
    return;
  }
  const typed = prompt(`恢复后将重新允许乘方受控生产写入。请完整输入：${phrase}`) || "";
  if (typed !== phrase) return;
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在解除生产紧停…";
  try {
    const result = await bridgeFetch("/chengfang/production-write/resume", {
      method: "POST",
      body: JSON.stringify({ confirm: true, confirmation_phrase: typed }),
    });
    applyChengfangProductionWrite(result);
  } catch (error) {
    setChengfangProductionError(error, "无法解除生产紧停；请先完成所有未知结果的官方对账。");
  } finally {
    button.textContent = "解除生产紧停";
    renderChengfangProductionWrite();
  }
});
document.getElementById("chengfang-production-target").addEventListener("change", () => {
  const budget = document.getElementById("chengfang-production-budget");
  budget.value = "";
  renderChengfangProductionWrite();
});
document.getElementById("chengfang-production-budget").addEventListener("input", renderChengfangProductionWrite);
document.getElementById("chengfang-production-confirmation").addEventListener("input", renderChengfangProductionWrite);
document.getElementById("chengfang-production-prepare").addEventListener("click", async (event) => {
  const draft = chengfangProductionBudgetDraft();
  if (!draft.valid || !draft.target?.target_key) return;
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在进行官方预检…";
  try {
    const result = await bridgeFetch("/chengfang/production-write/prepare", {
      method: "POST",
      body: JSON.stringify({ target_key: draft.target.target_key, target_budget: Number(draft.targetBudget.toFixed(2)) }),
    });
    const applied = applyChengfangProductionWrite(result);
    if (!applied.operation?.operation_id || chengfangProductionState(applied.operation, applied.status) !== "prepared") {
      throw Object.assign(new Error("生产预检未返回可授权操作"), { code: "PRODUCTION_PREFLIGHT_INCOMPLETE" });
    }
    document.getElementById("chengfang-production-authorization").scrollIntoView({ behavior: "smooth", block: "center" });
  } catch (error) {
    setChengfangProductionError(error, "预检失败；没有调用官方写接口。请刷新计划后重试。");
  } finally {
    button.textContent = "预检本次降预算";
    renderChengfangProductionWrite();
  }
});
document.getElementById("chengfang-production-authorize").addEventListener("click", async (event) => {
  const operation = currentChengfangProductionWrite.operation;
  const phrase = String(operation?.confirmation_phrase || "");
  const typed = document.getElementById("chengfang-production-confirmation").value;
  if (!operation?.operation_id || !phrase || typed !== phrase || chengfangProductionState(operation) !== "prepared") return;
  if (!confirm("确认授予本次单计划降预算短时权限？授权只绑定当前目标预算，尚不会执行写入。")) return;
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在签发一次性授权…";
  try {
    const result = await bridgeFetch("/chengfang/production-write/authorize", {
      method: "POST",
      body: JSON.stringify({ operation_id: operation.operation_id, confirmation_phrase: typed, confirm: true }),
    });
    applyChengfangProductionWrite(result);
  } catch (error) {
    setChengfangProductionError(error, "授权失败；没有执行官方写入。请核对确认词或重新预检。");
  } finally {
    button.textContent = "授予本次短时权限";
    renderChengfangProductionWrite();
  }
});
document.getElementById("chengfang-production-execute").addEventListener("click", async (event) => {
  const operation = currentChengfangProductionWrite.operation;
  if (!operation?.operation_id || chengfangProductionState(operation) !== "authorized"
      || (Number(operation.expires_at_ms || 0) > 0 && Date.now() > Number(operation.expires_at_ms))) return;
  if (!confirm(`最后确认：通过官方 API 将这一个计划的预算从 ${chengfangProductionMoney(operation.current_budget)} 降至 ${chengfangProductionMoney(operation.target_budget)}？点击后不会自动重试。`)) return;
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在执行并等待官方回读…";
  try {
    const result = await bridgeFetch("/chengfang/production-write/execute", {
      method: "POST",
      body: JSON.stringify({ operation_id: operation.operation_id }),
    });
    applyChengfangProductionWrite(result);
  } catch (error) {
    // A lost response can mean that the official platform accepted the write.
    // Never offer a retry: freeze locally until the Agent journal is reloaded
    // and the exact operation is reconciled through an official read.
    setChengfangProductionError(error, "执行响应中断，结果按未知处理；禁止重试，请刷新审计状态后人工核对。", { freeze: true });
  } finally {
    button.textContent = "执行一次官方降预算";
    renderChengfangProductionWrite();
  }
});
document.getElementById("chengfang-production-cancel").addEventListener("click", async (event) => {
  const operation = currentChengfangProductionWrite.operation;
  const phase = chengfangProductionState(operation);
  if (!operation?.operation_id || !["prepared", "authorized"].includes(phase) || phase === "unknown") return;
  if (!confirm("确认取消本次预检/授权并释放计划？取消只适用于尚未执行的操作；不会修改千川预算。")) return;
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在取消…";
  try {
    const result = await bridgeFetch("/chengfang/production-write/cancel", {
      method: "POST",
      body: JSON.stringify({ operation_id: operation.operation_id, confirm: true }),
    });
    applyChengfangProductionWrite(result);
  } catch (error) {
    setChengfangProductionError(error, "取消失败；请刷新操作状态。结果未知时不会允许取消。");
  } finally {
    button.textContent = "取消本次操作";
    renderChengfangProductionWrite();
  }
});
document.getElementById("chengfang-production-reconcile").addEventListener("click", async (event) => {
  const operation = currentChengfangProductionWrite.operation;
  if (!operation?.operation_id || chengfangProductionState(operation) !== "unknown") return;
  if (!confirm("请先在千川人工核对该计划。确认现在仅通过官方接口查询结果？本步骤不会再次写入，也不会点击网页。")) return;
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在只读查询官方结果…";
  try {
    const result = await bridgeFetch("/chengfang/production-write/reconcile", {
      method: "POST",
      body: JSON.stringify({ operation_id: operation.operation_id, confirm: true }),
    });
    applyChengfangProductionWrite(result);
  } catch (error) {
    setChengfangProductionError(error, "官方查询仍未给出唯一结果；生产写入继续冻结。", { freeze: true });
  } finally {
    button.textContent = "人工确认后查询官方结果";
    renderChengfangProductionWrite();
  }
});
document.getElementById("chengfang-one-click-demo-run").addEventListener("click", async (event) => {
  if (currentChengfangTrialConfig.environment === "production") return;
  if (!confirm("确认运行 1 分钟合成演示？它只生成 DEMO_FIXTURE 展示数据，不读取真实账户、不写平台，也不保存到真实 A2 runtime。")) return;
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在生成合成闭环…";
  try {
    const result = await bridgeFetch("/chengfang/a2-pilot/demo", {
      method: "POST",
      body: JSON.stringify({ confirm: true, environment: "demo" }),
    });
    if (result.synthetic !== true || result.demo_fixture !== true || result.platform_write_attempted !== false
        || result.runtime_persisted !== false || !result.runtime) {
      throw new Error("演示安全标记不完整，已拒绝展示");
    }
    currentChengfangDemoFixture = result;
    renderChengfangTrialConsole();
    document.getElementById("chengfang-trial-effect-heading").scrollIntoView({ behavior: "smooth", block: "center" });
  } catch (error) {
    currentChengfangDemoFixture = null;
    setChengfangTrialError(error, "合成演示生成失败，请确认本地 Agent 已启动。");
  } finally {
    button.disabled = false;
    button.textContent = "重新生成演示效果";
    renderChengfangOneClickDemo();
  }
});
document.getElementById("chengfang-one-click-demo-clear").addEventListener("click", () => {
  currentChengfangDemoFixture = null;
  renderChengfangTrialConsole();
});
document.getElementById("chengfang-trial-sync-toggle").addEventListener("click", async (event) => {
  const view = chengfangTrialView();
  const enable = !view.five_minute_sync_enabled;
  const message = enable
    ? "确认开启每 5 分钟同步？扩展只读取已经打开且已登录的抖店 / 千川页面，不会自动打开页面，也不会提交投放。"
    : view.master_enabled
      ? "关闭 5 分钟同步会先停止 A2 模拟，避免使用旧数据。确认继续？"
      : "确认关闭每 5 分钟同步？关闭后 A2 模拟配置与执行会被阻止。";
  if (!confirm(message)) return;
  const button = event.currentTarget;
  button.disabled = true;
  try {
    if (!enable && view.master_enabled) {
      const stopped = await bridgeFetch("/chengfang/a2-pilot/stop", {
        method: "POST",
        body: JSON.stringify({ confirm: true, reason: "AUTO_SYNC_DISABLED_FROM_EXTENSION" }),
      });
      applyChengfangA2Response(stopped);
    }
    const response = await chrome.runtime.sendMessage({
      type: "update-settings",
      settings: { autoSync: enable, intervalMinutes: 5 },
    });
    if (!response?.ok) throw new Error(response?.error || "扩展同步设置保存失败");
    currentExtensionSettings = {
      ...currentExtensionSettings,
      ...(response.settings || {}),
      autoSync: enable,
      intervalMinutes: 5,
    };
    renderChengfangTrialConsole();
  } catch (error) {
    setChengfangTrialError(error, "5 分钟同步设置失败，请重新加载扩展后再试。");
  } finally {
    button.disabled = false;
  }
});
document.getElementById("chengfang-trial-whitelist-add").addEventListener("click", async () => {
  const view = chengfangTrialView();
  if (!view.plan_key || view.master_enabled) return;
  await saveChengfangTrialConfig({ ...view.config, plan_whitelist: [view.plan_key] });
});
document.getElementById("chengfang-trial-pilot-budget-cap").addEventListener("change", async (event) => {
  const value = event.currentTarget.value === "" ? null : Number(event.currentTarget.value);
  await saveChengfangTrialConfig({ ...currentChengfangTrialConfig, budget_cap: value });
});
document.getElementById("chengfang-trial-edit-boundaries").addEventListener("click", () => {
  const profile = document.querySelector(".chengfang-profile");
  if (profile) profile.scrollIntoView({ behavior: "smooth", block: "start" });
  document.querySelector("[data-chengfang-boundary]")?.focus();
});
document.getElementById("chengfang-trial-start").addEventListener("click", async (event) => {
  const prepared = globalThis.DianChengfangTrialPolicy.buildConfigurePayload(
    chengfangTrialRuntime(),
    currentChengfangTrialConfig,
    currentExtensionSettings,
  );
  if (!prepared.ok) {
    document.getElementById("chengfang-trial-next").textContent = `还不能开启：${prepared.blockers.join("；")}`;
    return;
  }
  if (!confirm("确认开启 A2 本机模拟试运行？它只模拟降低总预算，并生成 simulation 回执；不会请求、填写或提交千川。")) return;
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const result = await bridgeFetch("/chengfang/a2-pilot/configure", {
      method: "POST",
      body: JSON.stringify(prepared.payload),
    });
    applyChengfangA2Response(result);
  } catch (error) {
    setChengfangTrialError(error, "A2 模拟试运行配置失败，请检查经营边界和作用域绑定。");
  } finally {
    renderChengfangTrialConsole();
  }
});
document.getElementById("chengfang-trial-review-accept").addEventListener("click", async (event) => {
  const view = chengfangTrialView();
  if (!view.can_accept || !view.candidate?.candidate_id) return;
  if (!confirm(`确认接受候选 ${shortOpaqueKey(view.candidate.candidate_id)} 进入本机模拟？此操作不会授权或提交千川。`)) return;
  event.currentTarget.disabled = true;
  try {
    const result = await bridgeFetch("/chengfang/a2-pilot/candidate/review", {
      method: "POST",
      body: JSON.stringify({ candidate_id: view.candidate.candidate_id, decision: "accept", note: "USER_ACCEPTED_SIMULATION_FROM_EXTENSION" }),
    });
    applyChengfangA2Response(result);
  } catch (error) {
    setChengfangTrialError(error, "候选审核失败，请刷新 A2 状态后重试。");
  } finally {
    renderChengfangTrialConsole();
  }
});
document.getElementById("chengfang-trial-review-reject").addEventListener("click", async (event) => {
  const view = chengfangTrialView();
  if (!view.can_reject || !view.candidate?.candidate_id) return;
  const note = prompt("可选：填写拒绝原因，便于后续复盘。", "当前候选不适合试运行");
  if (note === null) return;
  event.currentTarget.disabled = true;
  try {
    const result = await bridgeFetch("/chengfang/a2-pilot/candidate/review", {
      method: "POST",
      body: JSON.stringify({ candidate_id: view.candidate.candidate_id, decision: "reject", note }),
    });
    applyChengfangA2Response(result);
  } catch (error) {
    setChengfangTrialError(error, "候选拒绝失败，请刷新 A2 状态后重试。");
  } finally {
    renderChengfangTrialConsole();
  }
});
document.getElementById("chengfang-trial-execute").addEventListener("click", async (event) => {
  const view = chengfangTrialView();
  if (!view.can_execute || !view.candidate?.candidate_id) return;
  if (!confirm("确认执行本机模拟？本步骤使用 simulation 适配器，不会调用官方投放接口，也不会点击或填写千川页面。")) return;
  event.currentTarget.disabled = true;
  try {
    const result = await bridgeFetch("/chengfang/a2-pilot/candidate/execute", {
      method: "POST",
      body: JSON.stringify({ candidate_id: view.candidate.candidate_id, auto_readback: false }),
    });
    applyChengfangA2Response(result);
  } catch (error) {
    setChengfangTrialError(error, "本机模拟失败；A2 已按安全策略停止或保持阻止状态。");
  } finally {
    renderChengfangTrialConsole();
  }
});
document.getElementById("chengfang-trial-readback").addEventListener("click", async (event) => {
  const view = chengfangTrialView();
  if (!view.can_readback || !view.execution?.execution_id) return;
  event.currentTarget.disabled = true;
  try {
    const result = await bridgeFetch("/chengfang/a2-pilot/execution/readback", {
      method: "POST",
      body: JSON.stringify({ execution_id: view.execution.execution_id }),
    });
    applyChengfangA2Response(result);
  } catch (error) {
    setChengfangTrialError(error, "模拟回读失败；请查看停止原因，不要按真实投放结果理解。");
  } finally {
    renderChengfangTrialConsole();
  }
});
document.getElementById("chengfang-trial-stop").addEventListener("click", async (event) => {
  if (!confirm("确认紧急停止 A2 模拟并取消尚未执行的已接受候选？真实千川数据不会被修改。")) return;
  event.currentTarget.disabled = true;
  try {
    const result = await bridgeFetch("/chengfang/a2-pilot/stop", {
      method: "POST",
      body: JSON.stringify({ confirm: true, reason: "USER_REQUESTED_FROM_EXTENSION" }),
    });
    applyChengfangA2Response(result);
  } catch (error) {
    setChengfangTrialError(error, "紧急停止失败，请检查本地 Agent 状态。");
  } finally {
    renderChengfangTrialConsole();
  }
});
document.getElementById("chengfang-shadow-toggle").addEventListener("click", async () => {
  const planner = globalThis.DianChengfangPlanner;
  const button = document.getElementById("chengfang-shadow-toggle");
  const active = currentChengfangAgentRuntime
    ? currentChengfangAgentRuntime.decision_automation?.running === true
    : planner.buildShadowProgram(currentChengfangLocalPlan.shadow || {}).status === "active";
  button.disabled = true;
  try {
    const scope = currentChengfangProfileScope();
    const request = chengfangScopedPostPayload({ enabled: !active, profile: buildChengfangAgentProfile(scope) }, scope);
    const result = await bridgeFetch("/chengfang/autopilot/shadow", {
      method: "POST",
      body: JSON.stringify(request.body),
    });
    validateChengfangScopedResponse(scope, result);
    renderChengfangAgentRuntime(result.autopilot_runtime || {});
    currentChengfangLocalPlan.shadow = active
      ? { enabled: false, status: "inactive", stopped_at: Date.now(), days: currentChengfangLocalPlan.shadow?.days || [], execution_allowed: false, local_only: true }
      : planner.buildShadowProgram({ enabled: true, started_at: Date.now(), days: currentChengfangLocalPlan.shadow?.days || [] });
    await saveChengfangLocalPlan();
  } catch (error) {
    document.getElementById("chengfang-shadow-empty").textContent = error.message || "影子自动评估切换失败，请确认本地 Agent 已启动。";
  } finally {
    renderChengfangPlanner();
  }
});
document.getElementById("chengfang-evaluate-now").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在评估…";
  try {
    await evaluateChengfangNow("manual");
  } catch (error) {
    document.getElementById("chengfang-runtime-latest").textContent = error.message || "本次影子评估失败，真实投放未受影响。";
  } finally {
    button.textContent = "立即评估一次";
    button.disabled = currentChengfangAgentRuntime?.decision_automation?.running !== true;
  }
});
document.getElementById("chengfang-emergency-stop").addEventListener("click", async (event) => {
  if (!confirm("确认立即停止乘方自动评估并取消全部未处理影子候选？真实千川数据不会被修改。")) return;
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const scope = currentChengfangProfileScope();
    const request = chengfangScopedPostPayload({ confirm: true, reason: "USER_REQUESTED_FROM_EXTENSION" }, scope);
    const result = await bridgeFetch("/chengfang/autopilot/stop", {
      method: "POST",
      body: JSON.stringify(request.body),
    });
    validateChengfangScopedResponse(scope, result);
    renderChengfangAgentRuntime(result.autopilot_runtime || {});
    currentChengfangLocalPlan.shadow = { enabled: false, status: "inactive", stopped_at: Date.now(), days: currentChengfangLocalPlan.shadow?.days || [], execution_allowed: false, local_only: true };
    currentChengfangLocalPlanStore = upsertChengfangPlanForScope(currentChengfangLocalPlanStore, currentChengfangLocalPlan);
    await chrome.storage.local.set({ [CHENGFANG_LOCAL_PLAN_KEY]: currentChengfangLocalPlanStore });
    renderChengfangPlanner();
  } catch (error) {
    document.getElementById("chengfang-runtime-latest").textContent = error.message || "停止失败，请检查本地 Agent。";
  }
});
document.querySelectorAll("[data-chengfang-feedback]").forEach((button) => button.addEventListener("click", async (event) => {
  const container = document.getElementById("chengfang-runtime-feedback");
  const evaluationId = container.dataset.evaluationId || "";
  if (!evaluationId) return;
  container.querySelectorAll("button").forEach((item) => { item.disabled = true; });
  const target = event.currentTarget;
  const readback = target.dataset.readback === "true" ? true : target.dataset.readback === "false" ? false : null;
  try {
    const result = await bridgeFetch("/chengfang/autopilot/feedback", {
      method: "POST",
      body: JSON.stringify({
        evaluation_id: evaluationId,
        usefulness: target.dataset.chengfangFeedback,
        readback_success: readback,
        critical_incident: target.dataset.incident === "true",
      }),
    });
    renderChengfangAgentRuntime(result.autopilot_runtime || {});
    renderChengfangPlanner();
  } catch (error) {
    document.getElementById("chengfang-runtime-latest").textContent = error.message || "反馈保存失败，请稍后重试。";
    container.querySelectorAll("button").forEach((item) => { item.disabled = false; });
  }
}));
document.getElementById("refresh-button").addEventListener("click", () => refreshAll(false));
document.getElementById("sync-diagnose").addEventListener("click", () => refreshAll(true));
document.getElementById("dashboard-load-retry").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在重试…";
  try {
    await refreshAll(false);
  } catch (error) {
    renderDashboardLoadFailure(error);
  } finally {
    button.disabled = false;
    button.textContent = "只需重试加载";
  }
});
document.getElementById("chengfang-sync").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在同步…";
  try {
    await syncRecentQianchuanPage({
      expectedPageTypes: ["campaigns", "qianchuan_campaigns", "qianchuan_live"],
      purpose: "乘方计划同步",
    });
  } catch (_) {
    // The shared sync dock already presents the actionable error.
  } finally {
    button.disabled = false;
    button.textContent = "同步当前千川页";
  }
});
document.querySelectorAll("[data-autopilot-mode]").forEach((button) => button.addEventListener("click", async (event) => {
  const mode = event.currentTarget.dataset.autopilotMode;
  const definition = currentAutopilotCenterView?.modes?.find((item) => item.id === mode);
  if (!definition || definition.locked) return;
  currentAutopilotCenterSettings = globalThis.DianAutopilotCenter.normalizeSettings({
    ...currentAutopilotCenterSettings,
    mode,
  });
  await chrome.storage.local.set({ [AUTOPILOT_CENTER_SETTINGS_KEY]: currentAutopilotCenterSettings });
  renderAutopilotCenter();
  await recordAutopilotActivity("config", `预览${definition.label}策略包`, `${definition.description}；只改变本机预览，不会修改千川。`);
}));
document.getElementById("autopilot-package-apply").addEventListener("click", async (event) => {
  const preview = currentAutopilotPackagePreview;
  if (!preview?.can_apply || !preview.package) return;
  const message = `${preview.notice}\n\n影响 ${preview.affected_plan_count} 个匿名计划作用域，变化 ${preview.changed_count} 项。\n所有动作仍为不可执行草稿，不会修改千川。确认保存到本机？`;
  if (!confirm(message)) return;
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在保存…";
  try {
    const scopeKey = autopilotScopeKey();
    const saved = {
      ...preview.package,
      state: "local_applied",
      saved_at: Date.now(),
      local_only: true,
      platform_write_enabled: false,
      automatic_platform_submit: false,
    };
    currentAutopilotPackages = { ...currentAutopilotPackages, [scopeKey]: saved };
    await chrome.storage.local.set({ [AUTOPILOT_PACKAGES_KEY]: currentAutopilotPackages });
    await recordAutopilotActivity(
      "config",
      `保存${saved.mode_label}策略包 v${saved.revision}`,
      `覆盖 ${preview.changed_count} 项本机配置，作用于 ${preview.affected_plan_count} 个匿名计划；未触发平台写入。`,
    );
    renderAutopilotCenter();
  } finally {
    button.disabled = currentAutopilotPackagePreview?.can_apply !== true;
  }
});
document.getElementById("autopilot-center-primary").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const action = currentAutopilotCenterView?.primary_action?.id || button.dataset.action || "prepare_store";
  if (action === "prepare_store") {
    const idleLabel = button.textContent;
    button.disabled = true;
    button.textContent = "正在打开抖店…";
    try {
      await ensureCurrentStoreForScan(button);
      await loadDashboard();
      await recordAutopilotActivity("sync", "抖店已准备", "已采用当前抖店页面；没有手动识别或绑定步骤。 ");
    } catch (error) {
      document.getElementById("autopilot-center-next").textContent = simpleStorePreparationError(error);
    } finally {
      button.disabled = false;
      button.textContent = currentAutopilotCenterView?.primary_action?.label || idleLabel;
    }
    return;
  }
  if (action === "accounts") {
    navigateToWorkspaceElement("promotion-plan-center", { role: "直播投放", workspaceKey: "promotion-overview" });
    document.getElementById("promotion-account-filter")?.focus();
    await recordAutopilotActivity("review", "选择千川账户", "进入投放工作台选择本次管理的账户；未触发平台写入。 ");
    return;
  }
  if (action === "profile") {
    const nextAction = currentChengfangCandidatePath?.next_action || {};
    if (["goal", "costs", "boundaries", "evidence"].includes(nextAction.id)) revealChengfangCandidatePathTarget(nextAction);
    else navigateToWorkspaceElement("chengfang-profile", { role: "直播投放", workspaceKey: "autopilot-strategy" });
    await recordAutopilotActivity("review", "打开经营建档", nextAction.detail || "补齐利润、成本、证据和经营边界。 ");
    return;
  }
  if (action === "review") {
    navigateToWorkspaceElement("chengfang-trial-console", {
      role: "直播投放", workspaceKey: "autopilot-strategy",
      focusElement: document.getElementById("chengfang-trial-review-accept"),
    });
    await recordAutopilotActivity("review", "查看候选与模拟", "进入逐计划人工复核、模拟和回读链路。 ");
    return;
  }
  if (action === "shadow") {
    const nextAction = currentChengfangCandidatePath?.next_action?.id;
    const target = nextAction === "evaluate"
      ? document.getElementById("chengfang-evaluate-now")
      : document.getElementById("chengfang-shadow-toggle");
    navigateToWorkspaceElement("chengfang-shadow-program", {
      role: "直播投放", workspaceKey: "autopilot-strategy", focusElement: target, block: "center",
    });
    target?.click();
    await recordAutopilotActivity("shadow", nextAction === "evaluate" ? "触发一次影子评估" : "进入 A1 影子托管", "只生成候选，不打开、填写或提交千川页面。 ");
    return;
  }
  if (action !== "sync") return;
  const idleLabel = button.textContent;
  button.disabled = true;
  button.textContent = "正在同步并校验…";
  try {
    await syncRecentQianchuanPage({
      expectedPageTypes: ["campaigns", "qianchuan_campaigns", "qianchuan_live"],
      purpose: "乘方计划同步",
    });
    await recordAutopilotActivity("sync", "同步并校验当前千川页", "已刷新当前账户的只读快照和准备度。 ");
  } catch (_) {
    // The shared sync dock presents the actionable error and recovery path.
  } finally {
    button.disabled = false;
    button.textContent = currentAutopilotCenterView?.primary_action?.label || idleLabel;
  }
});
document.getElementById("autopilot-center-review").addEventListener("click", () => {
  navigateToWorkspaceElement("chengfang-trial-console", {
    role: "直播投放", workspaceKey: "autopilot-strategy",
    focusElement: document.getElementById("chengfang-trial-heading"),
  });
  recordAutopilotActivity("review", "打开候选与模拟", "查看当前候选生命周期和模拟回读。 ").catch(() => undefined);
});
document.getElementById("autopilot-center-advanced").addEventListener("click", () => {
  const fold = document.querySelector(".chengfang-status-fold");
  if (fold) fold.open = true;
  navigateToWorkspaceElement("chengfang-profile", {
    role: "直播投放", workspaceKey: "autopilot-strategy",
    focusElement: document.querySelector("[data-chengfang-boundary]"),
  });
  recordAutopilotActivity("review", "打开高级经营边界", "查看预算、止损、频率、冷却和授权期限。 ").catch(() => undefined);
});
document.querySelectorAll("[data-control-task-family]").forEach((button) => button.addEventListener("click", () => {
  currentControlTaskFamily = globalThis.DianControlTaskCenter.normalizeFamily(button.dataset.controlTaskFamily);
  renderControlTaskCenter();
}));
document.getElementById("control-task-primary").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const action = button.dataset.action || "sync";
  if (action === "open_builder") {
    const builder = document.getElementById("control-task-builder");
    builder.hidden = false;
    builder.open = true;
    const target = currentControlTaskFamily === "status"
      ? document.getElementById("control-task-current-status")
      : document.getElementById("control-task-current-duration");
    builder.scrollIntoView({ behavior: "smooth", block: "center" });
    target?.focus({ preventScroll: true });
    return;
  }
  if (action !== "import_candidate") {
    const idleLabel = button.textContent;
    button.disabled = true;
    button.textContent = "正在同步…";
    try {
      await syncRecentQianchuanPage({
        expectedPageTypes: ["campaigns", "qianchuan_campaigns", "qianchuan_live"],
        purpose: "控制任务同步",
      });
      await recordAutopilotActivity("control", `同步${currentControlTaskView?.selected_definition?.label || "控制任务"}数据`, "刷新只读证据；未触发平台写入。 ");
    } catch (_) {
      // The shared sync dock already exposes the recovery path.
    } finally {
      button.disabled = false;
      button.textContent = currentControlTaskView?.primary_action?.label || idleLabel;
    }
    return;
  }
  const candidateId = String(button.dataset.candidateId || "");
  if (!candidateId) return;
  if (!confirm("将当前 A1 降预算候选导入为一个独立的本机预算任务。\n\n不会打开、填写或提交千川页面，确认继续？")) return;
  button.disabled = true;
  button.textContent = "正在生成任务…";
  try {
    const result = await bridgeFetch("/chengfang/control-tasks/draft", {
      method: "POST",
      body: JSON.stringify({ candidate_id: candidateId }),
    });
    applyControlTaskResponse(result);
    await recordAutopilotActivity("control", "导入降预算控制任务", "从 A1 候选生成单变量本机任务；真实写入保持关闭。 ");
  } catch (error) {
    document.getElementById("control-task-safety").textContent = `任务生成失败：${error.message || "请重新同步后再试"}`;
  } finally {
    button.disabled = currentControlTaskView?.safe === false;
    button.textContent = currentControlTaskView?.primary_action?.label || "导入降预算候选";
  }
});
document.getElementById("control-task-builder-create").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const family = currentControlTaskFamily;
  if (!["status", "duration"].includes(family)) return;
  let operation;
  let currentValue;
  let targetValue;
  if (family === "status") {
    currentValue = document.getElementById("control-task-current-status").value;
    operation = document.getElementById("control-task-target-status").value;
    targetValue = { ENABLE: "ACTIVE", PAUSE: "PAUSED", CLOSE: "CLOSED" }[operation] || "";
    if (!currentValue) {
      document.getElementById("control-task-builder-note").textContent = "请先选择已经人工核对的当前状态。";
      return;
    }
  } else {
    currentValue = Number(document.getElementById("control-task-current-duration").value);
    targetValue = Number(document.getElementById("control-task-target-duration").value);
    if (!Number.isInteger(currentValue) || !Number.isInteger(targetValue) || currentValue <= 0 || targetValue <= 0 || currentValue === targetValue) {
      document.getElementById("control-task-builder-note").textContent = "请填写两个不同的正整数分钟数。";
      return;
    }
    operation = targetValue < currentValue ? "SHORTEN" : "EXTEND";
  }
  const warning = family === "status" && operation === "CLOSE"
    ? "“结束”是高风险目标：本机只会演练，不会删除任务，也不会提交千川。\n\n确认生成草稿？"
    : `将生成一个${family === "status" ? "状态" : "时长"}单变量本机草稿，不会提交千川。确认继续？`;
  if (!confirm(warning)) return;
  button.disabled = true;
  button.textContent = "正在生成…";
  try {
    const result = await bridgeFetch("/chengfang/control-tasks/draft", {
      method: "POST",
      body: JSON.stringify({
        use_current_plan: true,
        family,
        operation,
        current_value: currentValue,
        target_value: targetValue,
      }),
    });
    applyControlTaskResponse(result);
    document.getElementById("control-task-builder").open = false;
    await recordAutopilotActivity("control", `生成${family === "status" ? "状态" : "时长"}演练草稿`, `${currentValue} → ${targetValue}；单变量、本机模拟、未触发平台写入。`);
  } catch (error) {
    document.getElementById("control-task-builder-note").textContent = error.message || "草稿生成失败，请重新核对当前值。";
  } finally {
    button.disabled = false;
    button.textContent = `生成${family === "status" ? "状态" : "时长"}演练草稿`;
  }
});
document.getElementById("control-task-list").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-control-task-action]");
  if (!button) return;
  const action = button.dataset.controlTaskAction;
  const taskId = String(button.dataset.taskId || "");
  if (!taskId) return;
  const confirmations = {
    accept: "只批准这一个单变量任务进入本机模拟，不会提交千川。确认继续？",
    reject: "拒绝后该任务将结束，若需要需重新生成。确认拒绝？",
    simulate: "将运行本机模拟并生成模拟回执，不会连接千川写接口。确认继续？",
    readback: "将用模拟回执核对目标值，结果不代表千川已经修改。确认继续？",
  };
  if (!confirm(confirmations[action] || "确认继续？")) return;
  const endpoints = {
    accept: ["/chengfang/control-tasks/review", { task_id: taskId, decision: "accept", confirm: true }],
    reject: ["/chengfang/control-tasks/review", { task_id: taskId, decision: "reject" }],
    simulate: ["/chengfang/control-tasks/simulate", { task_id: taskId }],
    readback: ["/chengfang/control-tasks/readback", { task_id: taskId }],
  };
  const request = endpoints[action];
  if (!request) return;
  const idleLabel = button.textContent;
  button.disabled = true;
  button.textContent = "处理中…";
  try {
    const result = await bridgeFetch(request[0], { method: "POST", body: JSON.stringify(request[1]) });
    applyControlTaskResponse(result);
    const labels = { accept: "批准控制任务", reject: "拒绝控制任务", simulate: "运行控制任务模拟", readback: "核对控制任务回读" };
    await recordAutopilotActivity("control", labels[action] || "处理控制任务", `${taskId.slice(-8)} · 本机单变量链路；未触发平台写入。`);
  } catch (error) {
    document.getElementById("control-task-safety").textContent = `控制任务处理失败：${error.message || "请重新同步后再试"}`;
    button.disabled = false;
    button.textContent = idleLabel;
  }
});
document.getElementById("schedule-control-enabled").addEventListener("change", (event) => {
  scheduleDraftEnabled = event.currentTarget.checked === true;
  if (scheduleDraftEnabled && !scheduleDraftRanges.length) scheduleDraftRanges = [{ start: "09:00", end: "12:00" }];
  renderScheduleRanges();
});
document.getElementById("schedule-range-add").addEventListener("click", () => {
  if (scheduleDraftRanges.length >= 10) return;
  const minutes = (value) => Number(value.slice(0, 2)) * 60 + Number(value.slice(3));
  const occupied = scheduleDraftRanges
    .filter((item) => /^\d{2}:\d{2}$/.test(item.start) && /^\d{2}:\d{2}$/.test(item.end))
    .map((item) => [minutes(item.start), minutes(item.end)]);
  let candidate = null;
  for (let start = 0; start <= 22 * 60; start += 60) {
    const end = start + 120;
    if (occupied.every(([left, right]) => end <= left || start >= right)) {
      const format = (value) => `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
      candidate = { start: format(start), end: format(end) };
      break;
    }
  }
  if (!candidate) candidate = { start: "", end: "" };
  scheduleDraftRanges = [...scheduleDraftRanges, candidate];
  renderScheduleRanges();
  document.querySelector(`[data-schedule-index="${scheduleDraftRanges.length - 1}"][data-schedule-field="start"]`)?.focus();
});
document.getElementById("schedule-range-list").addEventListener("change", (event) => {
  const input = event.target.closest("input[data-schedule-index][data-schedule-field]");
  if (!input) return;
  const index = Number(input.dataset.scheduleIndex);
  const field = input.dataset.scheduleField;
  if (!Number.isInteger(index) || !["start", "end"].includes(field) || !scheduleDraftRanges[index]) return;
  scheduleDraftRanges = scheduleDraftRanges.map((item, itemIndex) => itemIndex === index ? { ...item, [field]: input.value } : item);
  renderScheduleRanges();
});
document.getElementById("schedule-range-list").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-schedule-remove]");
  if (!button) return;
  const index = Number(button.dataset.scheduleRemove);
  if (!Number.isInteger(index)) return;
  scheduleDraftRanges = scheduleDraftRanges.filter((_, itemIndex) => itemIndex !== index);
  renderScheduleRanges();
});
document.getElementById("schedule-control-save").addEventListener("click", async (event) => {
  const validation = validateScheduleDraft();
  if (!validation.valid || currentScheduleView?.scope_bound !== true || currentScheduleView?.unsafe === true) {
    renderScheduleRanges();
    return;
  }
  const actionLabel = scheduleDraftEnabled ? "启用每日时段演练" : "关闭每日时段演练";
  if (!confirm(`${actionLabel}并保存为新草稿？\n\n草稿仍需人工复核、模拟和回读；不会启动真实定时器，也不会提交千川。`)) return;
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在保存…";
  try {
    const result = await bridgeFetch("/chengfang/schedule-control/draft", {
      method: "POST",
      body: JSON.stringify({
        use_current_plan: true,
        enabled: scheduleDraftEnabled,
        time_ranges: scheduleDraftRanges.map((item) => ({ start: item.start, end: item.end })),
      }),
    });
    applyScheduleControlResponse(result);
    await recordAutopilotActivity("control", "保存定时启停草稿", `${actionLabel} · ${scheduleDraftRanges.length} 个时段 · 真实定时器与平台写入关闭。`);
  } catch (error) {
    const serverBlockers = error?.body?.revision?.validation?.blockers || [];
    const detail = serverBlockers.length ? serverBlockers.map(scheduleBlockerText).join("；") : error.message || "请重新同步后再试";
    const node = document.getElementById("schedule-control-validation");
    node.className = "schedule-control-validation danger";
    node.textContent = `保存失败：${detail}`;
  } finally {
    button.disabled = !validateScheduleDraft().valid || currentScheduleView?.scope_bound !== true;
    button.textContent = "保存定时启停草稿";
  }
});
document.getElementById("schedule-control-revision").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-schedule-action]");
  if (!button) return;
  const action = button.dataset.scheduleAction;
  const revisionId = String(button.dataset.revisionId || "");
  if (!revisionId) return;
  const confirmations = {
    accept: "只批准当前时间配置进入本机模拟，不会启动真实定时器或提交千川。确认继续？",
    reject: "拒绝后该草稿将结束；如需修改，请重新保存一个版本。确认拒绝？",
    simulate: "将生成未来 48 小时启用/暂停事件和本机模拟回执，不会连接千川写接口。确认继续？",
    readback: "将核对本机模拟事件指纹；通过只代表时间逻辑一致，不代表千川已执行。确认继续？",
  };
  if (!confirm(confirmations[action] || "确认继续？")) return;
  const endpoints = {
    accept: ["/chengfang/schedule-control/review", { revision_id: revisionId, decision: "accept", confirm: true }],
    reject: ["/chengfang/schedule-control/review", { revision_id: revisionId, decision: "reject" }],
    simulate: ["/chengfang/schedule-control/simulate", { revision_id: revisionId }],
    readback: ["/chengfang/schedule-control/readback", { revision_id: revisionId }],
  };
  const request = endpoints[action];
  if (!request) return;
  const idleLabel = button.textContent;
  button.disabled = true;
  button.textContent = "处理中…";
  try {
    const result = await bridgeFetch(request[0], { method: "POST", body: JSON.stringify(request[1]) });
    applyScheduleControlResponse(result);
    const labels = { accept: "批准定时启停演练", reject: "拒绝定时启停草稿", simulate: "运行 48 小时时段模拟", readback: "核对定时启停模拟回读" };
    await recordAutopilotActivity("control", labels[action] || "处理定时启停配置", `${revisionId.slice(-8)} · 本机时间编排；未触发平台写入。`);
  } catch (error) {
    const node = document.getElementById("schedule-control-validation");
    node.className = "schedule-control-validation danger";
    node.textContent = `处理失败：${error.message || "请重新同步后再试"}`;
    button.disabled = false;
    button.textContent = idleLabel;
  }
});
document.getElementById("autopilot-activity-filter").addEventListener("change", renderAutopilotActivity);
document.getElementById("autopilot-activity-search").addEventListener("input", renderAutopilotActivity);
document.getElementById("material-governance-save").addEventListener("click", async (event) => {
  const preview = currentMaterialGovernancePreview;
  if (!preview?.can_apply || !preview.package) return;
  const message = `${preview.notice}\n\n当前覆盖 ${preview.affected_material_count} 条素材：保护 ${preview.protected_count} 条、停测候选 ${preview.retire_candidate_count} 条、复测/测试 ${preview.retest_count} 条。\n规则包不保存素材名称，不会删除或修改千川素材。确认保存？`;
  if (!confirm(message)) return;
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在保存…";
  try {
    const scope = materialGovernanceScope();
    const scopeKey = globalThis.DianMaterialGovernance.fingerprint(scope);
    const saved = { ...preview.package, saved_at: Date.now(), local_only: true };
    currentMaterialGovernancePackages = { ...currentMaterialGovernancePackages, [scopeKey]: saved };
    await chrome.storage.local.set({ [MATERIAL_GOVERNANCE_PACKAGES_KEY]: currentMaterialGovernancePackages });
    await recordAutopilotActivity("config", "保存素材治理策略包", `规则 v${saved.revision} · 覆盖 ${preview.affected_material_count} 条素材 · 自动删除关闭。`);
    renderMaterialGovernance(currentCreativeAnalysis);
  } finally {
    button.disabled = currentMaterialGovernancePreview?.can_apply !== true;
  }
});
document.querySelectorAll("[data-promotion-view]").forEach((button) => button.addEventListener("click", () => {
  navigatePromotionView(button.dataset.promotionView || "overview");
}));
document.getElementById("operator-memory-refresh").addEventListener("click", () => loadOperatorMemory());
document.getElementById("ai-provider-select").addEventListener("change", () => {
  applyAiProviderDefaults();
});
document.getElementById("ai-base-url-input").addEventListener("input", () => {
  document.getElementById("ai-remote-consent").checked = false;
});
document.getElementById("ai-test-connection").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const payload = aiProviderPayload();
  if (!payload.model) {
    setAiMessage("ai-connection-message", "请先填写要使用的模型名称。", "warn");
    document.getElementById("ai-model-input").focus();
    return;
  }
  if (!aiRemoteConsentReady(payload)) {
    setAiMessage("ai-connection-message", "连接云端 AI 前，请先确认只发送上方预览中的脱敏聚合数据。", "warn");
    document.getElementById("ai-remote-consent").focus();
    return;
  }
  const idleLabel = button.textContent;
  button.disabled = true;
  button.textContent = "正在测试…";
  setAiMessage("ai-connection-message", "正在由本地 Agent 测试模型连接；不会发送店铺数据。", "");
  try {
    const result = await bridgeFetch("/ai/providers/test", { method: "POST", body: JSON.stringify(payload) });
    const latency = Number(result.latency_ms || 0);
    setAiMessage("ai-connection-message", `${result.message || "连接测试通过"}${latency > 0 ? ` · ${latency} ms` : ""}。保存后才会用于经营分析。`, "success");
  } catch (error) {
    setAiMessage("ai-connection-message", error.message || "连接测试失败，请核对模型、服务地址和密钥。", "error");
  } finally {
    button.disabled = false;
    button.textContent = idleLabel;
  }
});
document.getElementById("ai-provider-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = document.getElementById("ai-save-connection");
  const payload = aiProviderPayload();
  if (!payload.model) {
    setAiMessage("ai-connection-message", "请填写模型名称后再保存。", "warn");
    document.getElementById("ai-model-input").focus();
    return;
  }
  if (!aiRemoteConsentReady(payload)) {
    setAiMessage("ai-connection-message", "保存云端连接前，请先确认脱敏数据发送范围。", "warn");
    document.getElementById("ai-remote-consent").focus();
    return;
  }
  button.disabled = true;
  button.textContent = "正在保存…";
  setAiMessage("ai-connection-message", "正在交给本地 Agent 加密保存；页面不会保留密钥。", "");
  try {
    const result = await bridgeFetch("/ai/providers/configure", { method: "POST", body: JSON.stringify(payload) });
    document.getElementById("ai-api-key-input").value = "";
    setAiMessage("ai-connection-message", result.message || "连接已保存。AI 只能分析和生成影子建议。", "success");
    await Promise.all([loadAiStatus(), loadAiContextPreview()]);
  } catch (error) {
    setAiMessage("ai-connection-message", error.message || "保存失败，未修改原有连接。", "error");
  } finally {
    button.disabled = false;
    button.textContent = "保存连接";
  }
});
document.getElementById("ai-disable-connections").addEventListener("click", async (event) => {
  if (!confirm("停用全部 AI 分析连接？停用后不会再向模型发送经营数据；加密密钥仍保存在本机，之后可重新启用。")) return;
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在停用…";
  try {
    const result = await bridgeFetch("/ai/providers/disable-all", {
      method: "POST",
      body: JSON.stringify({ confirm: true }),
    });
    document.getElementById("ai-remote-consent").checked = false;
    setAiMessage("ai-connection-message", result.message || "全部 AI 连接已停用。", "success");
    await loadAiStatus();
  } catch (error) {
    setAiMessage("ai-connection-message", error.message || "停用失败，现有连接未改变。", "error");
  } finally {
    button.textContent = "停用全部 AI";
    button.disabled = !(Array.isArray(currentAiStatus.providers) && currentAiStatus.providers.some((item) => item?.enabled));
  }
});
document.getElementById("ai-refresh-context-preview").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "读取中…";
  await loadAiContextPreview();
  button.disabled = false;
  button.textContent = "刷新预览";
});
document.getElementById("ai-run-shadow").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  if (!aiStatusView(currentAiStatus).analysisAvailable) {
    setAiMessage("ai-shadow-message", "先测试并保存一个可用的 AI 连接。", "warn");
    return;
  }
  button.disabled = true;
  button.textContent = "影子分析中…";
  renderAiStatus({ ...currentAiStatus, shadow_running: true, analysis_available: true }, { hydrate: false });
  setAiMessage("ai-shadow-message", "AI 正在分析脱敏经营上下文；不会创建、暂停或调整任何投放计划。", "");
  try {
    const result = await bridgeFetch("/ai/shadow/run", {
      method: "POST",
      body: JSON.stringify({ store_key: selectedStoreKey || "", provider: document.getElementById("ai-provider-select").value }),
    });
    setAiMessage("ai-shadow-message", result.message || "影子分析已完成，建议仍处于未授权、不可执行状态。", "success");
    if (Array.isArray(result.items) || Array.isArray(result.proposals)) renderAiProposals(result);
    await Promise.all([loadAiStatus(), loadAiProposals()]);
    const list = document.getElementById("ai-proposal-list");
    list.hidden = false;
    document.getElementById("ai-view-proposals").textContent = "收起建议";
  } catch (error) {
    setAiMessage("ai-shadow-message", error.message || "影子分析失败；没有触发任何投放操作。", "error");
    await loadAiStatus();
  } finally {
    button.textContent = "运行影子分析";
    button.disabled = !aiStatusView(currentAiStatus).analysisAvailable;
  }
});
document.getElementById("ai-view-proposals").addEventListener("click", async (event) => {
  const list = document.getElementById("ai-proposal-list");
  const willOpen = list.hidden;
  list.hidden = !willOpen;
  event.currentTarget.textContent = willOpen ? "收起建议" : "查看建议";
  if (willOpen) await loadAiProposals();
});
document.getElementById("check-updates").addEventListener("click", () => runUpdateAction("/updates/check", "正在检查 Agent、扩展与知识包版本…"));
document.getElementById("apply-knowledge-update").addEventListener("click", () => runUpdateAction("/updates/apply", "正在验证并切换新的知识包…"));
document.getElementById("rollback-knowledge").addEventListener("click", () => runUpdateAction("/updates/rollback", "正在恢复上一个已验证的知识包…"));
document.getElementById("industry-pack-select").addEventListener("change", (event) => {
  const pack = currentIndustryPackSelection();
  if (!pack) return;
  renderIndustryPackPreview(pack);
  const message = document.getElementById("industry-pack-message");
  message.className = "industry-pack-message";
  message.textContent = String(pack.pack_id || "general") === "general"
    ? "将恢复为通用电商规则，只影响当前店铺；其他店铺的行业设置保持不变。"
    : `即将把“${pack.display_name}”应用到当前店铺，其他店铺不受影响。`;
});
document.getElementById("activate-industry-pack").addEventListener("click", async (event) => {
  const pack = currentIndustryPackSelection();
  const message = document.getElementById("industry-pack-message");
  const storeKey = selectedStoreKey || String(currentKnowledgeCatalog?.store_key || "");
  if (!pack || !storeKey) {
    message.className = "industry-pack-message warn";
    message.textContent = "请先选择当前店铺，再应用行业知识包。";
    return;
  }
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在应用…";
  message.className = "industry-pack-message";
  message.textContent = "正在保存本店行业设置并重新组合通用与行业规则…";
  try {
    const result = await bridgeFetch("/rules/packs/bind", {
      method: "POST",
      body: JSON.stringify({
        store_key: storeKey,
        pack_id: pack.pack_id || "general",
        pack_version: pack.pack_version || "",
      }),
    });
    const system = await loadSystemStatus();
    message.className = "industry-pack-message";
    message.textContent = `${result.message || "行业知识包已应用"}；建议现在重新巡检一次。`;
    if (!system) button.disabled = false;
  } catch (error) {
    message.className = "industry-pack-message error";
    message.textContent = error.message || "行业知识包应用失败，当前店铺仍使用原规则。";
    button.disabled = false;
    button.textContent = "应用到当前店铺";
  }
});
document.getElementById("import-industry-pack").addEventListener("click", () => document.getElementById("industry-pack-file").click());
document.getElementById("industry-pack-file").addEventListener("change", async (event) => {
  const file = event.currentTarget.files?.[0];
  if (!file) return;
  const message = document.getElementById("industry-pack-message");
  message.className = "industry-pack-message";
  if (file.size > 2 * 1024 * 1024) {
    message.className = "industry-pack-message error";
    message.textContent = "行业知识包不能超过 2 MB，未读取该文件。";
    event.currentTarget.value = "";
    return;
  }
  message.textContent = "正在验签并安装行业知识包；安装完成后仍需明确应用到当前店铺…";
  try {
    const pack = JSON.parse(await file.text());
    const result = await bridgeFetch("/rules/packs/import", { method: "POST", body: JSON.stringify({ pack }) });
    await loadSystemStatus();
    const ref = `${result.pack_id}|${result.pack_version}`;
    const select = document.getElementById("industry-pack-select");
    if ([...select.options].some((option) => option.value === ref)) {
      select.value = ref;
      renderIndustryPackPreview(currentIndustryPackSelection());
    }
    message.textContent = result.message || "行业知识包已安装；请确认后应用到当前店铺";
  } catch (error) {
    message.className = "industry-pack-message error";
    message.textContent = error.message || "行业知识包安装失败，当前店铺仍使用原规则";
  } finally {
    event.currentTarget.value = "";
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
    button.textContent = "同步官方数据";
    await refreshOceanEngineStatus().catch(() => { button.disabled = true; });
  }
});
document.getElementById("oauth-center-search").addEventListener("input", renderOceanEngineAccountCards);
document.getElementById("oauth-center-status-filter").addEventListener("change", renderOceanEngineAccountCards);
document.getElementById("oauth-center-group-filter").addEventListener("change", renderOceanEngineAccountCards);
document.getElementById("oauth-center-batch-action").addEventListener("change", () => {
  resetOceanEngineBatchPreview();
  renderOceanEngineSelection();
});
document.getElementById("oauth-center-list").addEventListener("change", (event) => {
  const selectKey = event.target?.dataset?.oauthAccountSelect;
  if (selectKey) {
    if (event.target.checked) oceanEngineAccountSelection.add(selectKey);
    else oceanEngineAccountSelection.delete(selectKey);
    resetOceanEngineBatchPreview("账户选择已变化，请重新预演本批影响。投放动作不会直接提交。 ");
    renderOceanEngineSelection();
    return;
  }
  const syncKey = event.target?.dataset?.oauthAccountSync;
  if (syncKey) {
    const card = event.target.closest(".oauth-center-account");
    const managed = card?.querySelector("[data-oauth-account-managed]");
    if (managed) {
      managed.disabled = !event.target.checked;
      if (!event.target.checked) managed.checked = false;
    }
  }
});
document.getElementById("oauth-center-list").addEventListener("click", async (event) => {
  const accountKey = event.target?.dataset?.oauthAccountSave;
  if (!accountKey) return;
  const button = event.target;
  const card = button.closest(".oauth-center-account");
  const result = document.getElementById("oauth-center-preview-result");
  button.disabled = true;
  button.textContent = "正在保存…";
  try {
    const body = DianOceanEngineAccountCenter.preferencePayload(accountKey, {
      alias: card.querySelector("[data-oauth-account-alias]")?.value,
      group_name: card.querySelector("[data-oauth-account-group]")?.value,
      sync_enabled: card.querySelector("[data-oauth-account-sync]")?.checked,
      managed: card.querySelector("[data-oauth-account-managed]")?.checked,
    });
    const response = await bridgeFetch("/oauth/oceanengine/account-center/preferences", {
      method: "POST",
      body: JSON.stringify(body),
    });
    renderOceanEngineAccountCenter(response.account_center || {});
    resetOceanEngineBatchPreview("账户设置已保存到本机；Token、平台密钥和原始账户 ID 均未进入扩展。 ");
    result.className = "oauth-center-preview-result ok";
  } catch (error) {
    result.textContent = `保存失败：${error.message}`;
    result.className = "oauth-center-preview-result error";
    button.disabled = false;
    button.textContent = "保存账户设置";
  }
});
document.getElementById("oauth-center-preview").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const result = document.getElementById("oauth-center-preview-result");
  button.disabled = true;
  button.textContent = "正在计算…";
  result.textContent = "正在核对授权、广告账户映射、读取能力与本地托管边界。";
  result.className = "oauth-center-preview-result";
  try {
    const response = await bridgeFetch("/oauth/oceanengine/account-center/preview", {
      method: "POST",
      body: JSON.stringify({
        account_keys: [...oceanEngineAccountSelection],
        action: document.getElementById("oauth-center-batch-action").value,
      }),
    });
    renderOceanEngineBatchPreview(response.preview || {});
  } catch (error) {
    result.textContent = `预演失败：${error.message}`;
    result.className = "oauth-center-preview-result error";
  } finally {
    button.textContent = "预演影响";
    renderOceanEngineSelection();
  }
});
document.getElementById("oauth-center-run-sync").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const result = document.getElementById("oauth-center-preview-result");
  if (currentOceanEngineBatchPreview?.action !== "sync") return;
  button.disabled = true;
  button.textContent = "正在只读同步…";
  result.textContent = "正在按已选账户读取官方计划、报表和素材；不会修改预算或计划状态。";
  result.className = "oauth-center-preview-result";
  try {
    const response = await bridgeFetch("/oauth/oceanengine/account-center/sync", {
      method: "POST",
      body: JSON.stringify({ account_keys: [...oceanEngineAccountSelection], days: 7 }),
    });
    renderOceanEngineSync(response.sync || {});
    renderOceanEngineAccountCenter(response.account_center || {});
    result.textContent = `只读同步完成：${Number(response.sync?.account_count || 0)} 个账户，保存 ${Number(response.sync?.saved_pages || 0)} 类数据；真实投放写入仍为关闭。`;
    result.className = "oauth-center-preview-result ok";
    currentOceanEngineBatchPreview = null;
    button.hidden = true;
  } catch (error) {
    result.textContent = `只读同步失败：${error.message}。已有本地快照不会丢失。`;
    result.className = "oauth-center-preview-result error";
  } finally {
    button.disabled = false;
    button.textContent = "确认只读同步";
  }
});
document.getElementById("connection-guide-action").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const action = button.dataset.action || currentConnectionGuide?.next_upgrade?.id || "none";
  if (["identify_store", "retry_identify_store", "confirm_store", "select_store", "start_store_scan"].includes(action)) {
    try {
      await runQuickScan(button, { purpose: "start_store_scan", pageIds: QUICK_SCAN_PAGE_IDS });
    } catch (error) {
      document.getElementById("scan-detail").textContent = simpleStorePreparationError(error);
      button.disabled = false;
      button.textContent = "打开抖店并开始";
    }
    return;
  }
  if (action === "review_account_link") {
    navigateToWorkspaceElement("promotion-plan-center", { role: "直播投放", workspaceKey: "promotion-overview" });
    return;
  }
  if (action === "quick_scan" || action === "refresh_core_data") {
    const refreshCoreData = action === "refresh_core_data";
    const refreshPageIds = globalThis.DianSimpleExperience
      .operationalTruth(currentConnectionGuide || {}).refreshPageIds;
    try {
      await runQuickScan(button, {
        purpose: refreshCoreData ? "refresh_core_data" : "quick_scan",
        pageIds: refreshCoreData ? refreshPageIds : QUICK_SCAN_PAGE_IDS,
      });
    } catch (error) {
      document.getElementById("scan-detail").textContent = error.message || (refreshCoreData ? "核心数据刷新未能启动" : "快速巡店未能启动");
    } finally {
      button.disabled = false;
      button.textContent = refreshCoreData
        ? currentConnectionGuide?.next_upgrade?.label || "刷新核心经营数据"
        : "开始 3 分钟快速巡店";
    }
    return;
  }
  if (action === "sync_qianchuan") {
    await syncRecentQianchuanPage({
      expectedPageTypes: ["campaigns", "qianchuan_campaigns", "qianchuan_live"],
      purpose: "投放数据同步",
    }).catch(() => undefined);
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
    if (experienceMode === "simple") document.body.classList.add("simple-show-receipt");
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
  const { ops, productGraph, shelf, live, creative, coverage } = currentOperationsContext;
  renderOperations(ops, productGraph, shelf, live, creative, coverage);
});
document.getElementById("next-best-action-button").addEventListener("click", (event) => {
  const emptyAction = event.currentTarget.dataset.emptyAction || "";
  if (["refresh_core_data", "quick_scan"].includes(emptyAction)) {
    document.getElementById("journey-primary-action")?.click();
    return;
  }
  const observationTaskId = event.currentTarget.dataset.observationTaskId || "";
  if (observationTaskId) {
    const observationButton = [...document.querySelectorAll("[data-observation-primary='true']")]
      .find((item) => item.dataset.observationTaskId === observationTaskId);
    if (observationButton) {
      observationButton.click();
      return;
    }
  }
  const target = document.getElementById(event.currentTarget.dataset.target || "manager-tasks");
  target?.scrollIntoView({ behavior: "smooth", block: "center" });
});
document.getElementById("preflight-reread").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在读取并复核…";
  try {
    if (["authorization_consumed", "manual_reconcile_required", "manual_reconcile_archived"].includes(currentPreflightState)) {
      const manual = await chrome.runtime.sendMessage({
        type: "manual-execution-readback",
        action_id: String(currentPreflightSession?.action_id || ""),
      });
      if (!manual?.ok) throw new Error(manual?.error || "独立回读失败");
      await refreshExecutionPreflight();
      document.getElementById("preflight-next").textContent = manual.verification?.verified
        ? "新的独立页面已确认目标值生效，动作已验收解锁。"
        : "已完成一次新的独立页面回读；结果仍不确定，原动作继续锁定，请勿重复提交。";
      return;
    }
    const sessionAction = currentPreflightAction || {};
    const rawPageType = String(sessionAction.page_type || "campaigns");
    const expectedPageType = rawPageType === "qianchuan_campaigns" ? "campaigns" : rawPageType;
    const response = await chrome.runtime.sendMessage({
      type: "sync-current-qianchuan",
      expected_account_key: String(
        sessionAction.account_key
        || sessionAction.target_ref?.account_key
        || selectedQianchuanAccount
        || "",
      ),
      expected_store_key: String(
        sessionAction.store_key
        || sessionAction.proposal_scope?.store_key
        || selectedStoreKey
        || "",
      ),
      expected_page_types: [expectedPageType],
      purpose: "execution_preflight_reread",
      require_hard_reload: true,
      session_started_at_ms: Number(currentPreflightSession?.started_at_ms || 0),
    });
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

document.getElementById("preflight-archive").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const actionId = String(currentPreflightSession?.action_id || "").trim().toLowerCase();
  const confirmationText = String(button.dataset.confirmationText || "");
  if (!/^[a-f0-9]{24}$/.test(actionId) || !confirmationText) {
    document.getElementById("preflight-next").textContent = "本地 Agent 未返回可归档动作或确认口令；原动作继续锁定。";
    return;
  }
  const entered = window.prompt(
    `归档不会把结果标记为成功或失败，只会释放其他计划；同一计划将永久禁止自动重投。\n请输入：${confirmationText}`,
    "",
  );
  if (entered === null) return;
  if (entered !== confirmationText) {
    document.getElementById("preflight-next").textContent = "归档口令不一致；未修改任何动作。";
    return;
  }
  button.disabled = true;
  button.textContent = "正在安全归档…";
  try {
    const response = await bridgeFetch("/actions/preflight/manual-reconcile/archive", {
      method: "POST",
      body: JSON.stringify({
        action_id: actionId,
        confirmation_text: entered,
        resolution_note: "用户在工作台显式归档；平台结果仍未知。",
      }),
    });
    renderExecutionPreflight(response.preflight || {});
  } catch (error) {
    document.getElementById("preflight-next").textContent = error.message || "归档失败；原动作继续锁定。";
    button.disabled = false;
  } finally {
    button.textContent = "归档未决动作";
  }
});

document.getElementById("preflight-authorize").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  if (!currentPreflightSession?.session_id) return;
  const confirmationText = String(button.dataset.confirmationText || "");
  if (!confirmationText) {
    document.getElementById("preflight-next").textContent = "本地 Agent 未返回最终确认口令，请刷新后重试；页面未提交。";
    return;
  }
  const entered = window.prompt(`这是最后一次人工授权。口令通过后将立即提交本次单计划操作。\n请输入：${confirmationText}`, "");
  if (entered === null) return;
  button.disabled = true;
  let outcomeMessage = "";
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
      document.getElementById("preflight-next").textContent = "浏览器已提交，正在按 2 / 5 / 15 / 30 秒多轮回读；动作保持锁定，请勿重复操作。";
      const execution = await chrome.runtime.sendMessage({
        type: "run-authorized-execution",
        authorization_id: authorizationId,
      });
      if (!execution?.ok) throw new Error(execution?.error || "预算受控执行失败");
      const readbackJob = execution.result?.readback_job || {};
      outcomeMessage = execution.result?.verification?.verified
        ? "预算变更已通过执行后的新页面回读验收。"
        : readbackJob.status === "failed"
          ? "多次独立页面回读确认本次提交未生效；重新同步后可以生成新方案。"
          : "多轮回读仍未确认平台结果，动作继续锁定；请勿重复执行，打开原计划页后再回读。";
    }
  } catch (error) {
    outcomeMessage = error.message || "最终授权失败";
  } finally {
    // Execution may move from authorized to consumed/manual-reconcile while
    // the 2/5/15/30-second readback loop runs. Always render canonical Agent
    // state so the manual reread button and emergency-stop lock are correct.
    try {
      await refreshExecutionPreflight();
    } catch (refreshError) {
      outcomeMessage = outcomeMessage || refreshError.message || "执行状态刷新失败，请重新打开工作台；请勿重复提交。";
    }
    if (outcomeMessage) document.getElementById("preflight-next").textContent = outcomeMessage;
    button.disabled = currentPreflightState !== "ready_for_final_confirmation";
  }
});
document.getElementById("full-scan-button").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  try {
    await ensureCurrentStoreForScan(button);
  } catch (error) {
    document.getElementById("scan-detail").textContent = simpleStorePreparationError(error);
    button.disabled = false;
    button.textContent = "开始全店巡检";
    return;
  }
  const plannedPageIds = DianAgentScanScopePolicy.fullScanPageIds({ includeAds: Boolean(selectedQianchuanAccount) });
  document.getElementById("scan-detail").textContent = selectedQianchuanAccount
    ? `本轮固定检查 ${DianAgentScanScopePolicy.scopes.full_doudian.length} 个抖店页面，并追加 ${DianAgentScanScopePolicy.scopes.ads_optional.length} 个千川页面。`
    : `本轮固定检查 ${DianAgentScanScopePolicy.scopes.full_doudian.length} 个抖店页面；连接千川后只会追加投放页面。`;
  const response = await chrome.runtime.sendMessage({
    type: "start-full-scan",
    scan_scope: "full",
    store_key: selectedStoreKey,
    account_key: selectedQianchuanAccount,
    page_ids: plannedPageIds,
  });
  if (!response?.ok) {
    document.getElementById("scan-detail").textContent = response?.error || "巡店未能启动，请检查本地 Agent。";
    button.disabled = false;
    return;
  }
  if (response.started === false) {
    document.getElementById("scan-detail").textContent = "已有巡店正在运行，已继续显示当前进度。";
  }
  await loadDashboard();
});
document.getElementById("product-graph-collect").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  let startedRunId = "";
  const kind = button.dataset.actionKind || "targeted_scan";
  if (kind === "focus_product") {
    const target = document.getElementById(button.dataset.targetId || "product-graph-first-recommendation");
    if (target) {
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      target.focus({ preventScroll: true });
      target.classList.add("module-highlight");
      setTimeout(() => target.classList.remove("module-highlight"), 1800);
    }
    return;
  }
  if (kind === "navigate") {
    const targetId = button.dataset.targetId === "connection-guide" ? "scan-card" : button.dataset.targetId || "scan-card";
    navigateToWorkspaceElement(targetId, { workspaceKey: targetId === "scan-card" ? "data-scan" : "" });
    return;
  }
  if (!selectedStoreKey) {
    try {
      await ensureCurrentStoreForScan(button);
    } catch (error) {
      document.getElementById("product-graph-note").textContent = simpleStorePreparationError(error);
      button.disabled = false;
      button.textContent = "打开抖店并同步商品";
      return;
    }
  }
  let pageIds;
  try {
    pageIds = JSON.parse(button.dataset.pageIds || "[]");
  } catch (_) {
    pageIds = [];
  }
  pageIds = (Array.isArray(pageIds) ? pageIds : [])
    .filter((pageId) => selectedQianchuanAccount || !String(pageId).startsWith("qianchuan_"));
  if (!pageIds.length) {
    document.getElementById("product-graph-note").textContent = "当前没有需要补充的商品页面。需要投放数据时，请进入“直播与投放”。";
    return;
  }
  button.disabled = true;
  button.textContent = "正在补齐单品链…";
  try {
    const response = await chrome.runtime.sendMessage({
      type: "start-full-scan",
      scan_scope: "product_graph",
      store_key: selectedStoreKey,
      account_key: selectedQianchuanAccount,
      page_ids: pageIds,
    });
    if (!response?.ok) throw new Error(response?.error || "单品链补采未能启动");
    if (response.started !== true || !response.run_id) {
      const error = new Error(response?.message || "已有巡检正在进行；请完成当前巡检后再补齐单品链");
      error.code = response?.code || "TARGETED_SCAN_NOT_STARTED";
      throw error;
    }
    startedRunId = String(response.run_id);
    await chrome.storage.local.set({
      scanReturnTarget: {
        target_id: "product-operating-graph",
        run_id: startedRunId,
        scope: "product_graph",
      },
    });
    navigateToWorkspaceElement("scan-card", { workspaceKey: "data-scan" });
    await loadDashboard();
  } catch (error) {
    button.disabled = false;
    button.textContent = "重新补齐单品链";
    document.getElementById("product-graph-note").textContent = startedRunId
      ? `补采 ${startedRunId.slice(-8)} 已启动，但工作台刷新失败：${error.message || "请稍后刷新；完成回执仍会保留。"}`
      : `补采启动失败：${error.message || "请确认本地 Agent 与店铺页面后重试。"}`;
  }
});
document.getElementById("qianchuan-account-select").addEventListener("change", async (event) => {
  const select = event.currentTarget;
  const previousStoreKey = selectedStoreKey;
  const previousAccountKey = selectedQianchuanAccount;
  const requestedStoreKey = String(select.value || "");
  select.disabled = true;
  // Store changes invalidate the old account immediately. Keeping it in local
  // state while /stores/select is in flight lets a fast second action combine
  // the new store with the previous store's Qianchuan account.
  selectedStoreKey = requestedStoreKey;
  selectedQianchuanAccount = "";
  switchChengfangLocalPlanScope(requestedStoreKey, "");
  accountSelectionRequired = !requestedStoreKey;
  await chrome.storage.local.set({ scanStorePreference: requestedStoreKey, scanAccountPreference: "" });
  try {
    await bridgeFetch("/stores/select", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
      body: JSON.stringify({ store_key: requestedStoreKey }),
    });
    await loadDashboard();
  } catch (error) {
    selectedStoreKey = previousStoreKey;
    selectedQianchuanAccount = previousAccountKey;
    switchChengfangLocalPlanScope(previousStoreKey, previousAccountKey);
    await chrome.storage.local.set({ scanStorePreference: previousStoreKey, scanAccountPreference: previousAccountKey });
    select.value = previousStoreKey;
    document.getElementById("qianchuan-account-hint").textContent = error.message || "店铺切换失败，已恢复原经营范围。";
  } finally {
    select.disabled = !agentWriteGateOpen();
  }
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
document.getElementById("link-account-button").addEventListener("click", async (event) => {
  const accountKey = document.getElementById("unlinked-account-select").value;
  if (!selectedStoreKey || !accountKey) return;
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await bridgeFetch("/stores/link", { method: "POST", headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" }, body: JSON.stringify({ store_key: selectedStoreKey, account_key: accountKey }) });
    await loadDashboard();
  } catch (error) {
    document.getElementById("qianchuan-account-hint").textContent = error.message || "账户关联失败，请重新核对店铺后再试。";
    button.disabled = false;
  }
});
document.getElementById("linked-account-select").addEventListener("change", (event) => {
  const accountKey = event.currentTarget.value;
  document.getElementById("select-linked-account-button").disabled = !accountKey || accountKey === selectedQianchuanAccount;
  document.getElementById("unlink-account-button").disabled = !accountKey;
  document.getElementById("store-linked-account-result").textContent = accountKey === selectedQianchuanAccount
    ? `本次已锁定匿名账户 ${accountKey.slice(-6).toUpperCase()}`
    : "选择后，本次巡检和投放判断只使用这个账户。";
});
document.getElementById("select-linked-account-button").addEventListener("click", async (event) => {
  const accountKey = document.getElementById("linked-account-select").value;
  if (!selectedStoreKey || !accountKey) return;
  const button = event.currentTarget;
  button.disabled = true;
  document.getElementById("store-linked-account-result").textContent = "正在切换本次巡检账户…";
  try {
    await bridgeFetch("/stores/select", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
      body: JSON.stringify({ store_key: selectedStoreKey, account_key: accountKey }),
    });
    selectedQianchuanAccount = accountKey;
    switchChengfangLocalPlanScope(selectedStoreKey, accountKey);
    await chrome.storage.local.set({ scanAccountPreference: accountKey });
    await loadDashboard();
  } catch (error) {
    button.disabled = false;
    document.getElementById("store-linked-account-result").textContent = error.message || "账户切换失败，请重新同步后再试。";
  }
});
document.getElementById("unlink-account-button").addEventListener("click", async (event) => {
  const accountKey = document.getElementById("linked-account-select").value;
  if (!selectedStoreKey || !accountKey) return;
  if (!confirm("确认解除当前店铺与这个匿名千川账户的关联？\n\n历史快照不会删除；解除后千川巡检与投放判断会暂停，直到重新关联。")) return;
  const button = event.currentTarget;
  button.disabled = true;
  document.getElementById("store-linked-account-result").textContent = "正在解除关联…";
  try {
    await bridgeFetch("/stores/unlink", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dian-Agent": "2" },
      body: JSON.stringify({ store_key: selectedStoreKey, account_key: accountKey }),
    });
    if (selectedQianchuanAccount === accountKey) {
      selectedQianchuanAccount = "";
      switchChengfangLocalPlanScope(selectedStoreKey, "");
    }
    await chrome.storage.local.set({ scanAccountPreference: "" });
    await loadDashboard();
  } catch (error) {
    button.disabled = false;
    document.getElementById("store-linked-account-result").textContent = error.message || "解除关联失败，请重试。";
  }
});
document.getElementById("unlinked-account-select").addEventListener("change", () => {
  renderSelectedQianchuanAccountEvidence();
});
document.getElementById("current-qianchuan-button").addEventListener("click", () => {
  syncRecentQianchuanPage().catch(() => undefined);
});
document.getElementById("qianchuan-sync-dock-button").addEventListener("click", () => {
  syncRecentQianchuanPage().catch(() => undefined);
});
document.getElementById("qianchuan-sync-dock-collapse").addEventListener("click", () => {
  const dock = document.getElementById("qianchuan-sync-dock");
  setQianchuanSyncDockCompact(!dock.classList.contains("compact"), { persist: true }).catch(() => undefined);
});
document.getElementById("qianchuan-sync-dock-close").addEventListener("click", () => {
  setQianchuanSyncDockHidden(true, { persist: true }).catch(() => undefined);
  document.getElementById("qianchuan-sync-dock-announcer").textContent = "千川悬浮同步已隐藏，可在顶部更多设置中恢复。";
  document.querySelector("#workspace-more-settings > summary")?.focus();
});
document.getElementById("qianchuan-sync-dock-restore").addEventListener("click", async () => {
  try {
    await setQianchuanSyncDockHidden(false, { persist: true });
    document.getElementById("workspace-more-settings").open = false;
    document.getElementById("qianchuan-sync-dock-announcer").textContent = "千川悬浮同步已恢复。";
    document.getElementById("qianchuan-sync-dock-button").focus();
  } catch (error) {
    document.getElementById("qianchuan-sync-dock-announcer").textContent = error?.message || "悬浮同步恢复失败，请重试。";
  }
});
document.getElementById("qianchuan-sync-dock").addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || document.getElementById("qianchuan-sync-dock").classList.contains("compact")) return;
  event.preventDefault();
  setQianchuanSyncDockCompact(true, { persist: true }).catch(() => undefined);
  document.getElementById("qianchuan-sync-dock-collapse").focus();
});
document.getElementById("material-library-sync").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在同步…";
  try {
    await syncRecentQianchuanPage({ expectedPageTypes: ["materials", "video_library"], purpose: "素材同步" });
    button.textContent = "同步完成";
  } catch (_) {
    button.textContent = "同步失败，请重试";
  } finally {
    setTimeout(() => {
      button.disabled = false;
      button.textContent = "同步素材";
    }, 1400);
  }
});
document.getElementById("live-pacing-sync").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const resultNode = document.getElementById("live-pacing-sync-result");
  button.disabled = true;
  button.textContent = "正在读取直播大屏…";
  resultNode.textContent = "正在核对页面类型、当前账户和数据时间；错误页面不会保存。";
  try {
    const result = await syncRecentQianchuanPage({ expectedPageTypes: ["live_dashboard", "qianchuan_live"], purpose: "直播数据同步" });
    const pacing = currentOperationsContext?.live?.pacing || {};
    const pageLabel = LABELS[result?.page_type] || result?.page_type || "直播大屏";
    const accountLabel = result?.account?.label ? ` · ${result.account.label}` : "";
    resultNode.textContent = `${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} 已读取${pageLabel}${accountLabel} · ${pacing.age_label || "刚刚更新"}。`;
  } catch (error) {
    resultNode.textContent = `读取失败：${error.message || "请打开当前账户的直播大屏后重试"}`;
  } finally {
    button.disabled = false;
    button.textContent = "读取当前直播大屏";
  }
});
document.getElementById("automation-sync-qianchuan").addEventListener("click", () => {
  syncRecentQianchuanPage({
    expectedPageTypes: ["campaigns", "qianchuan_campaigns", "qianchuan_live"],
    purpose: "投放执行准备同步",
  }).catch(() => undefined);
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
document.getElementById("retry-scan-button").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  if (button.dataset.overallRecovery === "true") {
    let pageIds = [];
    try { pageIds = JSON.parse(button.dataset.pageIds || "[]"); } catch (_) { pageIds = []; }
    const item = {
      error_code: button.dataset.errorCode || "",
      error: button.dataset.errorMessage || "",
      scope: button.dataset.scanScope || "full",
      targeted_page_ids: Array.isArray(pageIds) ? pageIds : [],
    };
    const recovery = scanRecoveryAction(item);
    button.disabled = true;
    try {
      await runScanRecovery(recovery, item, button);
    } catch (error) {
      document.getElementById("scan-summary").textContent = `恢复失败：${error?.message || "请核对当前页面后重试"}`;
      button.textContent = recovery.label;
    } finally {
      button.disabled = false;
    }
    return;
  }
  const response = await chrome.runtime.sendMessage({ type: "retry-failed-scan" });
  if (response?.ok !== true || response.started !== true) {
    document.getElementById("scan-summary").textContent = `继续失败：${response?.error || response?.message || "未能继续巡店"}`;
    return;
  }
  await loadDashboard();
});
function activateWorkspaceRole(role, activeButton = null) {
  if (!ROLE_WORKBENCH[role]) return;
  clearWorkspacePageFocus();
  currentRole = role;
  currentJourneyLane = role === "直播投放" ? "ads" : "store";
  managerQueueExpanded = false;
  document.querySelectorAll("#role-nav button[data-role]").forEach((item) => {
    item.classList.toggle("active", activeButton ? item === activeButton : item.dataset.role === currentRole);
  });
  document.querySelectorAll("#role-nav button[data-workspace-target]").forEach((item) => item.classList.remove("tool-active"));
  document.querySelectorAll("#role-nav .workspace-nav-tree").forEach((item) => item.classList.remove("has-active"));
  chrome.storage.local.set({ preferredRole: currentRole, [JOURNEY_LANE_KEY]: currentJourneyLane });
  renderWorkbench();
  if (currentOperationsContext) {
    const { ops, productGraph, shelf, live, creative, coverage } = currentOperationsContext;
    renderOperations(ops, productGraph, shelf, live, creative, coverage);
  } else applyModuleVisibility();
  if (currentJourneyInputs) renderJourneyCommand(currentJourneyInputs);
}

function setWorkspacePageContext(button) {
  const title = button.dataset.workspaceTitle;
  const subtitle = button.dataset.workspaceSubtitle;
  if (title) {
    document.getElementById("workspace-current-title").textContent = title;
    document.getElementById("workspace-page-title").textContent = title;
  }
  if (subtitle) document.getElementById("workspace-page-subtitle").textContent = subtitle;
  renderWorkspaceUserPath(button.dataset.workspaceTarget || "", title || document.getElementById("workspace-page-title").textContent);
  configureWorkspacePrimaryAction(button.dataset.workspaceTarget || "");
}

function configureWorkspacePrimaryAction(targetId = "") {
  const button = document.getElementById("workspace-primary-action");
  const journeyAction = currentJourneyCommand?.primaryAction;
  if (journeyAction) {
    button.textContent = journeyAction.label || "完成当前步骤";
    button.dataset.delegateTarget = "journey-primary-action";
    button.dataset.journeyActionId = journeyAction.id || "none";
    button.disabled = journeyAction.disabled === true || journeyAction.kind === "none";
    button.title = journeyAction.reason || currentJourneyCommand.detail || "当前经营旅程的唯一下一步";
    return;
  }
  const actions = {
    "connection-guide": ["检查连接", "connection-guide-action"],
    "product-operating-graph": ["同步商品", "product-graph-collect"],
    "content-workbench": ["同步素材", "material-library-sync"],
    "promotion-plan-center": ["同步投放", "promotion-plan-refresh"],
    "live-plan-management-section": ["同步直播", "live-pacing-sync"],
    "autopilot-center": ["同步千川", "qianchuan-sync-dock-button"],
    "automation-section": ["同步千川", "automation-sync-qianchuan"],
    "promotion-operation-log": ["刷新记录", "promotion-log-refresh"],
    "ai-connection-center": ["刷新发送预览", "ai-refresh-context-preview"],
  };
  const [label, delegateTarget] = actions[targetId] || ["3 分钟快速巡店", "quick-scan"];
  button.textContent = label;
  button.dataset.delegateTarget = delegateTarget;
  delete button.dataset.journeyActionId;
  button.disabled = false;
  button.removeAttribute("title");
}

function setWorkspaceNavTreeExpanded(tree, expanded) {
  if (!tree) return;
  tree.classList.toggle("is-expanded", expanded);
  tree.querySelector(":scope > .workspace-nav-parent")?.setAttribute("aria-expanded", String(expanded));
}

function toggleWorkspaceNavTree(button) {
  const tree = button.closest(".workspace-nav-tree");
  if (!tree) return;
  const shouldExpand = !tree.classList.contains("is-expanded");
  document.querySelectorAll("#role-nav .workspace-nav-tree").forEach((item) => setWorkspaceNavTreeExpanded(item, item === tree && shouldExpand));
}

function clearWorkspacePageFocus() {
  document.body.classList.remove("workspace-tool-focus");
  document.querySelectorAll(".panel-shell > .workspace-focus-hidden").forEach((element) => {
    element.classList.remove("workspace-focus-hidden");
  });
  document.querySelectorAll(".workspace-subfocus-hidden").forEach((element) => element.classList.remove("workspace-subfocus-hidden"));
  document.querySelectorAll(".workspace-subfocus-root").forEach((element) => element.classList.remove("workspace-subfocus-root"));
}

function focusWorkspacePage(target) {
  const panel = document.querySelector(".panel-shell");
  if (!panel || !target) return;
  const businessModule = Boolean(target.closest?.(".module-section"));
  document.querySelectorAll(".workspace-subfocus-hidden").forEach((element) => element.classList.remove("workspace-subfocus-hidden"));
  document.querySelectorAll(".workspace-subfocus-root").forEach((element) => element.classList.remove("workspace-subfocus-root"));
  let page = target;
  while (page.parentElement && page.parentElement !== panel) page = page.parentElement;
  if (page.parentElement !== panel) return;
  document.body.classList.add("workspace-tool-focus");
  [...panel.children].forEach((element) => {
    const keep = element === page
      || element.classList.contains("workspace-page-heading")
      || element.id === "agent-offline-banner"
      || (!businessModule && (
        element.classList.contains("workspace-user-path")
        || element.id === "journey-command-center"
        || element.id === "priority-reminder"
      ));
    element.classList.toggle("workspace-focus-hidden", !keep);
  });
  if (target !== page) {
    page.classList.add("workspace-subfocus-root");
    let current = target;
    while (current && current !== page) {
      const parent = current.parentElement;
      if (!parent) break;
      [...parent.children].forEach((sibling) => {
        const keepContext = sibling.tagName === "HEADER"
          || sibling.tagName === "SUMMARY"
          || sibling.classList.contains("section-heading")
          || sibling.classList.contains("version-section-heading")
          || sibling.classList.contains("promotion-mode-nav");
        if (sibling !== current && !keepContext) sibling.classList.add("workspace-subfocus-hidden");
      });
      current = parent;
    }
  }
}

function openWorkspaceTargetAncestors(target) {
  let details = target?.tagName === "DETAILS" ? target : target?.closest?.("details");
  while (details) {
    details.open = true;
    details = details.parentElement?.closest?.("details") || null;
  }
}

function workspaceSidebarButton(targetId, key = "") {
  if (key) {
    const byKey = document.querySelector(`#role-nav button[data-workspace-key="${CSS.escape(key)}"]`);
    if (byKey) return byKey;
  }
  return [...document.querySelectorAll("#role-nav button[data-workspace-target]")]
    .find((item) => item.dataset.workspaceTarget === targetId) || null;
}

const SIMPLE_WORKSPACE_REVEAL_ROUTES = Object.freeze([
  ["simple-show-batch", "batch-plan-preparation"],
  ["simple-show-receipt", "scan-receipt-card"],
  ["simple-show-industry", "industry-pack-center"],
  ["simple-show-ai", "ai-connection-center"],
  ["simple-show-report", "report-center-card"],
  ["simple-show-settings", "strategy-settings-card"],
  ["simple-show-version", "data-version-center"],
  ["simple-show-oauth", "oceanengine-oauth-card"],
  ["simple-show-production", "chengfang-production-write"],
]);

function revealSimpleWorkspaceTarget(target) {
  if (experienceMode !== "simple" || !target) return;
  const targetId = target.id || "";
  SIMPLE_WORKSPACE_REVEAL_ROUTES.forEach(([className, routeId]) => {
    const matches = targetId === routeId || Boolean(target.closest?.(`#${routeId}`));
    document.body.classList.toggle(className, matches);
  });
}

function navigateToWorkspaceElement(targetOrId, options = {}) {
  const requestedRole = options.role || "";
  if (requestedRole) activateWorkspaceRole(requestedRole, options.roleButton || null);
  const target = typeof targetOrId === "string" ? document.getElementById(targetOrId) : targetOrId;
  if (!target) return false;
  const targetId = target.id || options.targetId || "";
  if (targetId === "connection-guide") {
    target.hidden = false;
    target.setAttribute("aria-hidden", "false");
  }
  clearWorkspacePageFocus();
  const section = target.closest?.(".module-section");
  if (section) section.hidden = false;
  revealSimpleWorkspaceTarget(target);
  openWorkspaceTargetAncestors(target);
  currentWorkspaceTarget = targetId;
  const navigationButton = options.navigationButton || workspaceSidebarButton(targetId, options.workspaceKey || "");
  if (navigationButton) {
    setWorkspacePageContext(navigationButton);
    const navTree = navigationButton.closest(".workspace-nav-tree");
    if (navTree) {
      document.querySelectorAll("#role-nav .workspace-nav-tree").forEach((item) => setWorkspaceNavTreeExpanded(item, item === navTree));
    }
  } else if (options.title || options.subtitle) {
    if (options.title) {
      document.getElementById("workspace-current-title").textContent = options.title;
      document.getElementById("workspace-page-title").textContent = options.title;
    }
    if (options.subtitle) document.getElementById("workspace-page-subtitle").textContent = options.subtitle;
    renderWorkspaceUserPath(targetId, options.title || "");
  }
  document.querySelectorAll("#role-nav button[data-workspace-target]").forEach((item) => {
    item.classList.toggle("tool-active", item === navigationButton);
  });
  if (navigationButton?.matches?.("[data-simple-nav]")) {
    document.querySelectorAll("#role-nav [data-simple-nav]").forEach((item) => {
      item.classList.toggle("active", item === navigationButton);
    });
  } else if (navigationButton?.closest?.("#role-nav") && !navigationButton.dataset.workspaceRole) {
    document.querySelectorAll("#role-nav [data-simple-nav]").forEach((item) => item.classList.remove("active"));
  }
  document.querySelectorAll("#role-nav .workspace-nav-tree").forEach((item) => item.classList.toggle("has-active", Boolean(item.querySelector(".tool-active"))));
  const focusTarget = options.focusTarget || target;
  if (targetId !== "priority-reminder") focusWorkspacePage(focusTarget);
  const scrollTarget = options.scrollTarget
    || (targetId === "industry-pack-center" ? target.previousElementSibling : null)
    || target;
  requestAnimationFrame(() => {
    scrollTarget.scrollIntoView({ behavior: options.behavior || "smooth", block: options.block || "start" });
    options.focusElement?.focus?.({ preventScroll: true });
    if (options.highlight) {
      target.classList.remove("module-highlight");
      requestAnimationFrame(() => target.classList.add("module-highlight"));
      setTimeout(() => target.classList.remove("module-highlight"), 1800);
    }
  });
  return true;
}

function navigateWorkspaceTarget(button) {
  const targetId = button.dataset.workspaceTarget;
  const target = document.getElementById(targetId);
  if (!target) return;
  const promotionView = button.dataset.workspaceView || (targetId === "promotion-mode-workbench" ? "chengfang" : "");
  if (promotionView) setPromotionView(promotionView);
  if (button.dataset.planFilterMode || button.dataset.planFilterType) {
    currentPromotionPlanFilters = globalThis.DianPromotionPlanCenter.normalizeFilters({
      ...currentPromotionPlanFilters,
      mode: button.dataset.planFilterMode || currentPromotionPlanFilters.mode,
      plan_type: button.dataset.planFilterType || currentPromotionPlanFilters.plan_type,
    });
    renderPromotionPlanConsole();
    persistPromotionPlanFilters().catch(() => undefined);
  }
  if (targetId === "batch-plan-preparation") {
    const mode = button.dataset.workspaceMode || promotionView;
    currentBatchPlanMode = ["full_domain", "chengfang", "suixintui"].includes(mode) ? mode : "chengfang";
    document.getElementById("batch-plan-mode-label").textContent = {
      full_domain: "全域推广 · 批量创建",
      chengfang: "乘方推广 · 批量创建",
      suixintui: "随心推 · 批量创建",
    }[currentBatchPlanMode];
    document.getElementById("batch-plan-review-summary").textContent = `${promotionPlanModeLabel(currentBatchPlanMode)} · 补齐账户、推广对象和经营参数后生成本地草稿。`;
  }
  return navigateToWorkspaceElement(target, {
    role: button.dataset.workspaceRole || button.dataset.role || "",
    roleButton: button.matches?.("[data-role]") ? button : null,
    navigationButton: button.closest("#role-nav") ? button : null,
    workspaceKey: button.dataset.workspaceKey || "",
  });
}

[
  "promotion-plan-account-filter",
  "promotion-plan-mode-filter",
  "promotion-plan-type-filter",
  "promotion-plan-status-filter",
  "promotion-plan-binding-filter",
].forEach((id) => document.getElementById(id).addEventListener("change", readPromotionPlanFilters));
document.getElementById("promotion-plan-search").addEventListener("input", readPromotionPlanFilters);
document.getElementById("promotion-plan-feature-filters").addEventListener("change", readPromotionPlanFilters);
document.getElementById("promotion-plan-coverage-action").addEventListener("click", async (event) => {
  await runPromotionPlanCollectionRecovery(event.currentTarget);
});
document.getElementById("promotion-plan-refresh").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在同步…";
  try {
    await refreshPromotionPlanConsole({ syncPage: true, planType: currentPromotionPlanFilters.plan_type });
    button.textContent = "已刷新";
  } catch (error) {
    document.getElementById("promotion-plan-notice").textContent = `刷新失败：${error.message || "请检查本地 Agent 与千川页面"}`;
    button.textContent = "刷新失败";
  } finally {
    setTimeout(() => { button.disabled = false; button.textContent = "同步并刷新"; }, 1400);
  }
});
document.getElementById("promotion-plan-table-body").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-promotion-plan-action]");
  if (!button) return;
  button.disabled = true;
  const idleLabel = button.textContent;
  button.textContent = button.dataset.promotionPlanAction === "unbind" ? "正在解除…" : "正在绑定…";
  try {
    await togglePromotionPlanBinding(button.dataset.planKey, button.dataset.promotionPlanAction);
  } catch (error) {
    document.getElementById("promotion-plan-notice").textContent = `本地绑定失败：${error.message || "请补齐账户和计划身份"}`;
    button.disabled = false;
    button.textContent = idleLabel;
  }
});
document.getElementById("promotion-plan-table-body").addEventListener("change", (event) => {
  const input = event.target.closest("input[data-promotion-plan-select]");
  if (!input) return;
  if (input.checked && selectedPromotionPlanKeys.size >= 50 && !selectedPromotionPlanKeys.has(input.value)) {
    input.checked = false;
    document.getElementById("promotion-plan-notice").textContent = "一次最多选择 50 个计划；请先完成或清空当前批次。";
    return;
  }
  if (input.checked) selectedPromotionPlanKeys.add(input.value);
  else selectedPromotionPlanKeys.delete(input.value);
  resetPromotionBulkActionDraft();
  renderPromotionPlanSelection();
});
document.getElementById("promotion-plan-select-all").addEventListener("change", (event) => {
  const visibleKeys = (currentPromotionPlanView?.rows || [])
    .slice(0, 200)
    .filter((row) => row.supervised_draft_ready === true)
    .map((row) => row.plan_key);
  if (event.currentTarget.checked) {
    visibleKeys.forEach((key) => {
      if (selectedPromotionPlanKeys.size < 50) selectedPromotionPlanKeys.add(key);
    });
    if (visibleKeys.length > 50) document.getElementById("promotion-plan-notice").textContent = "已选择当前结果中的前 50 个计划。";
  } else {
    visibleKeys.forEach((key) => selectedPromotionPlanKeys.delete(key));
  }
  resetPromotionBulkActionDraft();
  renderPromotionPlanSelection();
});
document.getElementById("promotion-plan-clear-selection").addEventListener("click", () => {
  selectedPromotionPlanKeys.clear();
  resetPromotionBulkActionDraft();
  renderPromotionPlanSelection();
});
document.getElementById("promotion-plan-open-bulk").addEventListener("click", () => {
  navigateToWorkspaceElement("promotion-bulk-action-center", {
    role: "直播投放",
    title: "批量操作",
    subtitle: "多选计划后先检查影响并生成预演，再逐项人工复核。",
  });
});
document.getElementById("promotion-bulk-back-to-plans").addEventListener("click", () => {
  navigateToWorkspaceElement("promotion-plan-center", {
    role: "直播投放",
    title: "直播与投放",
    subtitle: "统一管理直播与商品投放计划。",
  });
});
document.getElementById("promotion-bulk-selected-list").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-promotion-bulk-remove]");
  if (!button) return;
  selectedPromotionPlanKeys.delete(button.dataset.promotionBulkRemove);
  resetPromotionBulkActionDraft();
  renderPromotionPlanSelection();
});
document.getElementById("promotion-bulk-action-type").addEventListener("change", () => {
  syncPromotionBulkActionControls();
  resetPromotionBulkActionDraft();
  renderPromotionBulkSelection();
});
["promotion-bulk-decrease-percent", "promotion-bulk-schedule-start", "promotion-bulk-schedule-end"].forEach((id) => {
  document.getElementById(id).addEventListener("input", () => {
    if (currentPromotionBulkActionDraft) {
      resetPromotionBulkActionDraft();
      renderPromotionBulkSelection();
    }
  });
});
document.getElementById("promotion-bulk-generate").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在逐项检查…";
  try {
    const draft = await generatePromotionBulkActionDraft();
    button.textContent = draft.summary.blocked ? "已生成，包含阻塞项" : "已生成安全预演";
  } catch (error) {
    const state = document.getElementById("promotion-bulk-action-state");
    state.className = "danger";
    state.textContent = "预演生成失败";
    document.getElementById("promotion-bulk-notice").textContent = error.message || "请检查计划与动作参数。";
    button.textContent = "生成失败";
  } finally {
    setTimeout(() => {
      button.disabled = !selectedPromotionPlanKeys.size;
      button.textContent = "生成批量动作预演";
    }, 1400);
  }
});
document.getElementById("promotion-bulk-copy").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  if (!currentPromotionBulkActionDraft) return;
  const draft = currentPromotionBulkActionDraft;
  const lines = [
    `${draft.action_label}｜${draft.summary.total} 项｜可复核 ${draft.summary.ready}｜阻塞 ${draft.summary.blocked}`,
    ...draft.items.map((item, index) => `${index + 1}. [${item.state === "blocked" ? "阻塞" : "待复核"}] ${item.account_label} / ${item.plan_name}｜${item.field}：${promotionBulkValue(item.current_value)} → ${promotionBulkValue(item.target_value)}${item.blockers.length ? `｜${item.blockers.join("；")}` : ""}`),
    `安全说明：${draft.notice}`,
  ];
  try {
    await navigator.clipboard.writeText(lines.join("\n"));
    button.textContent = "已复制复核清单";
  } catch {
    button.textContent = "复制失败";
  }
  setTimeout(() => { button.textContent = "复制复核清单"; }, 1300);
});

["promotion-log-operation-filter", "promotion-log-state-filter", "promotion-log-date-from", "promotion-log-date-to"].forEach((id) => {
  document.getElementById(id).addEventListener("change", readPromotionOperationLogFilters);
});
document.getElementById("promotion-log-search").addEventListener("input", readPromotionOperationLogFilters);
document.getElementById("promotion-log-refresh").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在刷新…";
  try {
    await refreshPromotionOperationLog();
    button.textContent = "日志已刷新";
  } catch (error) {
    document.getElementById("promotion-operation-log-time").textContent = error.message || "刷新失败，请检查本地 Agent";
    button.textContent = "刷新失败";
  } finally {
    setTimeout(() => { button.disabled = false; button.textContent = "刷新日志"; }, 1300);
  }
});
document.getElementById("promotion-log-export").addEventListener("click", (event) => {
  if (!currentPromotionOperationLogView?.rows?.length) return;
  const content = globalThis.DianPromotionOperationLog.toCsv(currentPromotionOperationLogView.rows);
  const url = URL.createObjectURL(new Blob([content], { type: "text/csv;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `店策Agent-投放操作日志-${new Date().toISOString().slice(0, 10)}.csv`;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  const button = event.currentTarget;
  button.textContent = "已导出 CSV";
  setTimeout(() => { button.textContent = "导出 CSV"; }, 1200);
});
document.getElementById("promotion-log-clear-filters").addEventListener("click", () => {
  document.getElementById("promotion-log-operation-filter").value = "all";
  document.getElementById("promotion-log-state-filter").value = "all";
  document.getElementById("promotion-log-date-from").value = "";
  document.getElementById("promotion-log-date-to").value = "";
  document.getElementById("promotion-log-search").value = "";
  readPromotionOperationLogFilters();
});
document.getElementById("promotion-plan-batch-shortcut").addEventListener("click", openSimpleBatchPlan);
document.getElementById("batch-plan-generate").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在生成…";
  try {
    await generatePromotionBatchDraft();
    button.textContent = "已生成本地草稿";
  } catch (error) {
    const result = document.getElementById("batch-plan-result");
    result.className = "batch-plan-result empty-state";
    result.textContent = `无法生成：${error.message || "请检查输入"}`;
    document.getElementById("batch-plan-review-summary").textContent = error.message || "请检查账户、推广对象和经营参数。";
    button.textContent = "生成失败";
  } finally {
    setTimeout(() => { button.disabled = false; button.textContent = "生成批量计划草稿"; }, 1400);
  }
});
document.getElementById("batch-plan-copy").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  if (!currentPromotionBatchDraft) return;
  try {
    await navigator.clipboard.writeText(JSON.stringify(currentPromotionBatchDraft, null, 2));
    button.textContent = "已复制草稿 JSON";
  } catch {
    button.textContent = "复制失败";
  }
  setTimeout(() => { button.textContent = "复制草稿 JSON"; }, 1200);
});

document.querySelectorAll("[data-experience-mode-value]").forEach((button) => button.addEventListener("click", () => {
  applyExperienceMode(button.dataset.experienceModeValue);
  renderSimpleJourney();
  document.querySelector(".workspace-page-heading")?.scrollIntoView({ behavior: "smooth", block: "start" });
}));
document.querySelectorAll("[data-journey-lane]").forEach((button) => button.addEventListener("click", async () => {
  currentJourneyLane = button.dataset.journeyLane === "ads" ? "ads" : "store";
  await chrome.storage.local.set({ [JOURNEY_LANE_KEY]: currentJourneyLane });
  renderJourneyCommand();
}));
document.getElementById("journey-primary-action").addEventListener("click", () => {
  runJourneyPrimaryAction().catch(() => undefined);
});
document.getElementById("simple-start-action").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const action = button.dataset.action || currentSimpleJourney?.action?.id || "none";
  if (action === "none") return;
  if (action === "view_today") {
    document.getElementById("next-best-action")?.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  if (action === "retry_dashboard") {
    button.disabled = true;
    button.textContent = "正在重新连接…";
    await refreshAll(false);
    return;
  }
  if (action === "repair_agent") {
    button.disabled = true;
    button.textContent = "正在打开修复指引…";
    try {
      await openAgentRepairGuide();
      button.textContent = "修复后点“重新检测”";
    } catch (error) {
      button.textContent = error?.message || "修复指引打开失败";
    } finally {
      button.disabled = false;
    }
    return;
  }
  if (action === "open_extension_manager") {
    button.disabled = true;
    button.textContent = "正在打开扩展管理页…";
    try {
      const response = await chrome.runtime.sendMessage({ type: "open-extension-manager" });
      if (!response?.ok) throw new Error(response?.error || "扩展管理页打开失败");
      button.textContent = "请在新页面点“重新加载”";
    } catch (error) {
      button.textContent = error?.message || "扩展管理页打开失败";
    } finally {
      button.disabled = false;
    }
    return;
  }
  if (["start_store_scan", "quick_scan", "refresh_core_data"].includes(action)) {
    button.disabled = true;
    try {
      await runQuickScan(button, {
        purpose: action,
        pageIds: action === "refresh_core_data"
          ? currentSimpleJourney?.action?.page_ids
          : QUICK_SCAN_PAGE_IDS,
      });
    } catch (error) {
      document.getElementById("simple-start-next-detail").textContent = error?.message || "巡店未能启动，请打开抖店首页后重试。";
    } finally {
      button.disabled = false;
      button.textContent = currentSimpleJourney?.action?.label || (action === "refresh_core_data" ? "刷新经营数据" : "开始巡店");
    }
    return;
  }
  const guideButton = document.getElementById("connection-guide-action");
  guideButton.dataset.action = action;
  guideButton.click();
});

document.getElementById("role-nav").addEventListener("click", (event) => {
  const navToggle = event.target.closest("button[data-nav-toggle]");
  if (navToggle) {
    toggleWorkspaceNavTree(navToggle);
    return;
  }
  const button = event.target.closest("button[data-role]");
  if (!button) return;
  event.stopPropagation();
  navigateWorkspaceTarget(button);
});
document.querySelector(".workspace-shell").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-workspace-target]");
  if (button) {
    navigateWorkspaceTarget(button);
    const moreSettings = button.closest("#workspace-more-settings");
    if (moreSettings) moreSettings.open = false;
  }
});
document.addEventListener("click", (event) => {
  if (agentWriteGateOpen()) return;
  const control = event.target.closest?.("[data-agent-write]");
  if (!control) return;
  event.preventDefault();
  event.stopImmediatePropagation();
}, true);
document.getElementById("sidepanel-repair-agent").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在打开…";
  try {
    if (button.dataset.action === "retry_connection") {
      button.textContent = "正在重新检测…";
      await refreshAll(false);
      button.textContent = agentWriteGateOpen() ? "连接已恢复" : "仍在恢复，请稍后重试";
    } else if (button.dataset.action === "open_extension_manager") {
      const response = await chrome.runtime.sendMessage({ type: "open-extension-manager" });
      if (!response?.ok) throw new Error(response?.error || "扩展管理页打开失败");
      button.textContent = "请在新页面点“重新加载”";
    } else {
      await openAgentRepairGuide();
      button.textContent = "修复指引已打开";
    }
  } catch (error) {
    document.getElementById("agent-offline-detail").textContent = error?.message || "恢复入口打开失败，请从安装包运行 Repair Dian Agent。";
    button.textContent = button.dataset.action === "retry_connection"
      ? "重试连接"
      : button.dataset.action === "open_extension_manager" ? "重试打开扩展管理页" : "重试打开修复指引";
  } finally {
    button.disabled = false;
  }
});
document.getElementById("sidepanel-retry-agent").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = "正在检测…";
  try {
    await refreshAll(false);
  } finally {
    button.disabled = false;
    button.textContent = "重新检测";
  }
});
document.getElementById("workspace-nav-toggle").addEventListener("click", (event) => {
  const shell = document.querySelector(".workspace-shell");
  const collapsed = shell.classList.toggle("sidebar-collapsed");
  event.currentTarget.setAttribute("aria-expanded", String(!collapsed));
});
document.getElementById("workspace-primary-action").addEventListener("click", async (event) => {
  const delegateTarget = document.getElementById(document.getElementById("workspace-primary-action").dataset.delegateTarget || "");
  if (delegateTarget && !delegateTarget.disabled) {
    delegateTarget.click();
    return;
  }
  try {
    await runQuickScan(event.currentTarget);
  } catch (error) {
    document.getElementById("scan-detail").textContent = error.message || "快速巡店未能启动";
  } finally {
    configureWorkspacePrimaryAction(currentWorkspaceTarget);
  }
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
