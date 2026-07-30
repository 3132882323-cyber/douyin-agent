/** 任务卡片、处置队列与货架/直播/素材任务渲染 */

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
    高潜: summary.high_potential_videos || 0,
  });
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
  const owner = String(item.owner || "运营").replace("运营总管", "总管").replace("运营", "");
  const queuePrefix = options.queueIndex ? `第 ${options.queueIndex} 项 · ` : "";
  meta.textContent = `${queuePrefix}${item.level === "high" ? "立即处理" : item.level === "opportunity" ? "增长机会" : "今日处理"} · ${owner || "运营"}`;
  const title = document.createElement("strong"); title.textContent = item.title || "运营任务";
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
  card.append(meta, title, action, chips, detail);
  appendCopyAction(card, item.action_params);
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
    .filter((item) => item.status !== "done" && (currentRole === "运营总管" || item.owner === currentRole) && (opportunity ? item.level === "opportunity" : item.level !== "opportunity"))
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
    ["待观察", items.filter((item) => item.status === "observing").length, "observing"],
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

function renderOperations(ops, shelf, live, creative, coverage = []) {
  currentOps = ops;
  currentOperationsContext = { ops, shelf, live, creative, coverage };
  const allTasks = roleTasks(ops, false);
  const allGrowth = roleTasks(ops, true);
  const visibleTasks = managerQueueExpanded ? allTasks : allTasks.slice(0, 3);
  const expand = document.getElementById("manager-expand");
  document.getElementById("task-heading").textContent = currentRole === "运营总管" ? "今日处置队列" : `${currentRole} · 今日处置队列`;
  document.getElementById("manager-queue-caption").textContent = currentRole === "运营总管"
    ? "跨岗位按紧急程度排列，先处理风险，再进入观察。"
    : "按紧急程度排列，完成动作后再进入观察。";
  document.getElementById("manager-count").textContent = `${allTasks.length} 项待处理`;
  expand.hidden = allTasks.length <= 3;
  expand.textContent = managerQueueExpanded ? "收起队列" : `查看全部 ${allTasks.length} 项`;
  renderQueueStats(allTasks);
  renderTasks("manager-tasks", visibleTasks, { queue: true, showModuleLink: true });
  document.getElementById("growth-count").textContent = `${allGrowth.length} 项`;
  renderTasks("growth-tasks", allGrowth.slice(0, 3), { showModuleLink: true });
  const scoped = (ops.all_tasks || []).filter((item) => currentRole === "运营总管" || item.owner === currentRole);
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

