/** 千川计划 / 止损队列 / 库存建议渲染 */

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
    action.textContent = item.can_start_execution ? "可进入逐次授权的受监督执行" : `当前为${report.execution_mode_label || "观察模式"}，先由运营复核`;
    card.append(top, reason, components, saving, action);
    appendCopyAction(card, item.action_params);
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

