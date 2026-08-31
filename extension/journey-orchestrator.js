(function initJourneyOrchestrator(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.DianJourneyOrchestrator = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function journeyOrchestratorFactory() {
  "use strict";

  const ACTIVE_AUTH_STATES = new Set([
    "authorized",
    "authorizing",
    "executing",
    "running",
    "submitted",
    "awaiting_confirmation",
    "ready_for_final_confirmation",
  ]);
  const ACTIVE_READBACK_STATES = new Set([
    "awaiting_reread",
    "awaiting_readback",
    "reading_back",
    "verifying",
  ]);
  const READY_BOUNDARY_STATES = new Set([
    "ready",
    "preflight_ready",
    "confirmable",
    "authorized",
    "authorizing",
    "executing",
    "running",
    "submitted",
    "awaiting_confirmation",
    "ready_for_final_confirmation",
    "awaiting_reread",
    "awaiting_readback",
    "reading_back",
    "verifying",
  ]);
  const TERMINAL_TASK_STATES = new Set(["done", "completed", "cancelled", "closed", "verified"]);
  const EVALUATED_EFFECT_STATES = new Set(["effective", "ineffective", "rollback_recommended"]);

  function object(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  }

  function list(value) {
    return Array.isArray(value) ? value.filter(Boolean) : [];
  }

  function text(value) {
    return String(value ?? "").trim();
  }

  function key(value) {
    return text(value).toLowerCase();
  }

  function number(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : 0;
  }

  function normalizeLane(value) {
    const normalized = key(value);
    return /^(ads?|promotion|qianchuan|chengfang|live_ads)$/.test(normalized)
      || /投放|千川|乘方/.test(normalized)
      ? "ads"
      : "store";
  }

  function firstState(source) {
    const value = object(source);
    return key(
      value.state
      || value.status
      || object(value.session).state
      || object(value.action).state
      || object(object(value.session).action).state,
    );
  }

  function itemAccountKey(item) {
    const value = object(item);
    return key(
      value.account_key
      || value.accountKey
      || object(value.target_ref).account_key
      || object(value.targetRef).accountKey
      || object(object(value.action).target_ref).account_key
      || object(object(value.session).action).account_key,
    );
  }

  function messages(values) {
    const result = [];
    list(values).forEach((item) => {
      const value = text(object(item).message || object(item).label || item);
      if (value && !result.includes(value)) result.push(value);
    });
    return result;
  }

  function evidence(id, label, status, value) {
    return { id, label, status, value: text(value) };
  }

  function blocker(code, message, targetId) {
    return { code, message, targetId };
  }

  function action(id, kind, label, targetId, reason) {
    return { id, kind, label, targetId, reason, disabled: false };
  }

  function stage(id, label, status, summary) {
    return { id, label, status, summary: text(summary) };
  }

  function activePreflightAccountKey(preflight) {
    const state = firstState(preflight);
    return ACTIVE_AUTH_STATES.has(state) || ACTIVE_READBACK_STATES.has(state)
      ? itemAccountKey(preflight)
      : "";
  }

  function selectedAccountCandidate(value) {
    const item = object(value);
    return key(
      item.selectedAccountKey
      || item.selected_account_key
      || item.accountKey
      || item.account_key,
    );
  }

  function distinctScopeValues(values) {
    return [...new Set(values.map(key).filter(Boolean))];
  }

  function storeScopeConflict(inputs, onboarding, connectionGuide, operationContext) {
    const sources = [object(inputs), onboarding, connectionGuide, operationContext];
    const storeIds = distinctScopeValues(sources.map((item) => item.storeId || item.store_id));
    const storeKeys = distinctScopeValues(sources.map((item) => item.storeKey || item.store_key));
    return storeIds.length > 1 || storeKeys.length > 1;
  }

  function accountScopeValues(inputs, onboarding, operationContext, planConsole, preflight) {
    return distinctScopeValues([
      selectedAccountCandidate(inputs),
      selectedAccountCandidate(onboarding),
      selectedAccountCandidate(operationContext),
      selectedAccountCandidate(planConsole),
      activePreflightAccountKey(preflight),
    ]);
  }

  function identityConflict(inputs, onboarding, connectionGuide, operationContext, planConsole, selectedAccountKey, preflight) {
    const contextState = firstState(operationContext);
    const guideState = firstState(connectionGuide);
    const explicit = onboarding.identity_conflict === true
      || connectionGuide.identity_conflict === true
      || operationContext.identity_conflict === true
      || operationContext.blocked === true
      || connectionGuide.blocked === true
      || [contextState, guideState].some((value) => [
        "blocked",
        "identity_blocked",
        "identity_conflict",
        "scope_conflict",
        "store_conflict",
        "account_conflict",
      ].includes(value));
    const preflightAccount = activePreflightAccountKey(preflight);
    const accountMismatch = Boolean(selectedAccountKey && preflightAccount && selectedAccountKey !== preflightAccount);
    const activeWithoutAccount = Boolean(
      (ACTIVE_AUTH_STATES.has(firstState(preflight)) || ACTIVE_READBACK_STATES.has(firstState(preflight)))
      && !selectedAccountKey
      && !preflightAccount,
    );
    const analysisBlocked = onboarding.store_confirmed === true && operationContext.analysis_allowed === false;
    const accountConflict = accountScopeValues(inputs, onboarding, operationContext, planConsole, preflight).length > 1;
    return explicit || accountMismatch || accountConflict
      || storeScopeConflict(inputs, onboarding, connectionGuide, operationContext)
      || activeWithoutAccount || analysisBlocked;
  }

  function conflictMessages(inputs, onboarding, connectionGuide, operationContext, planConsole, selectedAccountKey, preflight) {
    const result = [
      ...messages(onboarding.blockers),
      ...messages(connectionGuide.blockers),
      ...messages(connectionGuide.blocked_reasons),
      ...messages(operationContext.blockers),
      ...messages(operationContext.blocked_reasons),
    ];
    const preflightAccount = activePreflightAccountKey(preflight);
    if (storeScopeConflict(inputs, onboarding, connectionGuide, operationContext)) {
      result.unshift("工作台各模块的数据范围不一致");
    }
    if (accountScopeValues(inputs, onboarding, operationContext, planConsole, preflight).length > 1) {
      result.unshift("工作台各模块返回的千川账户范围不一致");
    }
    if (selectedAccountKey && preflightAccount && selectedAccountKey !== preflightAccount) {
      result.unshift("当前授权会话与所选千川账户不一致");
    }
    if (!selectedAccountKey && !preflightAccount
      && (ACTIVE_AUTH_STATES.has(firstState(preflight)) || ACTIVE_READBACK_STATES.has(firstState(preflight)))) {
      result.unshift("进行中的授权会话缺少千川账户身份");
    }
    return [...new Set(result)].filter(Boolean);
  }

  function resolveSelectedAccount(inputs, onboarding, operationContext, planConsole, preflight) {
    const explicit = key(
      inputs.selectedAccountKey
      || inputs.selected_account_key
      || planConsole.selectedAccountKey
      || planConsole.selected_account_key
      || operationContext.selectedAccountKey
      || operationContext.selected_account_key
      || operationContext.account_key
      || onboarding.selectedAccountKey
      || onboarding.selected_account_key,
    );
    return explicit || activePreflightAccountKey(preflight);
  }

  function resolvePlanRows(planConsole, selectedAccountKey) {
    const rows = list(planConsole.rows).filter((row) => row && typeof row === "object");
    if (!selectedAccountKey) return rows;
    // A row without an account identity is not evidence for the selected
    // account. Falling back to untagged rows can carry a previous account's
    // plan freshness and eligibility into the current execution journey.
    return rows.filter((row) => itemAccountKey(row) === selectedAccountKey);
  }

  const CORE_PAGE_IDS = new Set(["overview", "orders", "products", "shelf"]);

  function operationalTruth(connectionGuide) {
    const guide = object(connectionGuide);
    const operational = object(guide.operational);
    const state = key(operational.state || "setup_required");
    const connectedLevel = text(operational.connected_level || guide.level || "L0").toUpperCase();
    const coreDataFresh = operational.core_data_fresh === true;
    const rawPageIds = Array.isArray(operational.refresh_page_ids)
      ? operational.refresh_page_ids
      : Array.isArray(object(guide.next_upgrade).page_ids) ? object(guide.next_upgrade).page_ids : [];
    const refreshPageIds = [...new Set(rawPageIds.map(text).filter((item) => CORE_PAGE_IDS.has(item)))];
    let dataState = "never";
    if (coreDataFresh) dataState = "fresh";
    else if (["loading", "checking", "refreshing"].includes(state)) dataState = "loading";
    else if (["failed", "error"].includes(state)) dataState = "failed";
    else if (["refresh_required", "stale", "expired"].includes(state)
      || object(guide.next_upgrade).id === "refresh_core_data"
      || /^L[2-9]$/.test(connectedLevel)) dataState = "stale";
    return { state, connectedLevel, coreDataFresh, refreshPageIds, dataState };
  }

  function dataProblem(operational) {
    if (operational.dataState === "failed") return "最近一次核心数据读取失败，需要按失败页面重新采集。";
    if (operational.dataState === "stale") {
      return "经营数据已经过期，需要刷新后再判断";
    }
    if (operational.dataState === "loading") return "正在核对核心经营页，完成前不会把空结果当成经营正常。";
    return "还没有足够的店铺数据，需要先完成一次巡店";
  }

  function resolveTasks(ops) {
    const sources = [
      ops.today_top_actions,
      ops.all_tasks,
      ops.items,
      ops.tasks,
      ops.today_tasks,
      ops.today_queue,
      ops.queue,
    ];
    const tasks = sources.find(Array.isArray) || [];
    return tasks.filter((item) => {
      const value = object(item);
      return !TERMINAL_TASK_STATES.has(key(value.state || value.status));
    });
  }

  function firstTask(ops) {
    const tasks = resolveTasks(ops);
    const startable = tasks.find((item) => object(object(item).task_contract).eligibility?.can_start !== false
      && object(item).eligibility?.can_start !== false);
    return startable || tasks[0] || null;
  }

  function taskCount(ops) {
    const tasks = resolveTasks(ops);
    if (tasks.length) return tasks.length;
    const summary = object(ops.summary);
    return number(summary.pending || summary.todo || summary.total || ops.pending_count || ops.total);
  }

  function hasPendingReadback(effectiveness) {
    return list(effectiveness.items).some((item) => [
      "pending",
      "awaiting_reread",
      "awaiting_readback",
      "observing",
      "measuring",
    ].includes(key(object(item).status || object(item).state)));
  }

  function evaluatedCount(effectiveness) {
    const items = list(effectiveness.items);
    const fromItems = items.filter((item) => EVALUATED_EFFECT_STATES.has(key(object(item).status || object(item).state))).length;
    return fromItems || number(object(effectiveness.summary).evaluated || effectiveness.evaluated);
  }

  function planFacts(planConsole, selectedAccountKey, capability) {
    const rows = resolvePlanRows(planConsole, selectedAccountKey);
    const summary = object(planConsole.summary);
    // Global totals do not prove that the selected account has any rows.
    const total = selectedAccountKey ? rows.length : rows.length || number(summary.total || planConsole.total);
    const consoleFreshness = key(planConsole.freshness_status || planConsole.freshness);
    const staleRows = rows.filter((row) => object(row).stale === true
      || ["stale", "expired", "missing"].includes(key(object(row).freshness_status || object(row).data_quality))).length;
    const fresh = total > 0 && staleRows === 0 && !["stale", "expired", "missing"].includes(consoleFreshness);
    const carriesIdentity = rows.some((row) => Object.prototype.hasOwnProperty.call(object(row), "eligible_for_local_binding"));
    const identityStage = list(capability.stages).find((item) => key(object(item).id) === "identity");
    const identityBlockers = rows.flatMap((row) => [
      ...messages(object(row).binding_blockers),
      ...messages(object(row).identity_blockers),
    ]);
    const identityReady = fresh && identityBlockers.length === 0 && (
      carriesIdentity
        ? rows.some((row) => object(row).eligible_for_local_binding === true)
        : key(object(identityStage).status) === "ready"
          || ["local_management", "supervised_ready", "closed_loop"].includes(key(capability.level))
    );
    return { rows, total, staleRows, fresh, identityReady, identityBlockers: [...new Set(identityBlockers)] };
  }

  function boundariesReady(preflight, capability, operationContext) {
    const boundaries = object(preflight.boundaries || preflight.guardrails || preflight.risk_boundary);
    const explicit = [
      preflight.boundaries_ready,
      preflight.guardrails_ready,
      preflight.risk_boundary_ready,
      preflight.policy_ready,
      preflight.limits_confirmed,
      boundaries.ready,
      boundaries.confirmed,
      operationContext.execution_boundary_confirmed,
    ];
    if (explicit.some((value) => value === false)) return false;
    if (explicit.some((value) => value === true)) return true;
    const actionStage = list(capability.stages).find((item) => key(object(item).id) === "action");
    return READY_BOUNDARY_STATES.has(firstState(preflight))
      || key(object(actionStage).status) === "ready"
      || object(capability.truth).execution_enabled === true;
  }

  function activeJourney(preflight, effectiveness) {
    const state = firstState(preflight);
    if (ACTIVE_READBACK_STATES.has(state) || hasPendingReadback(effectiveness)) return "readback";
    if (ACTIVE_AUTH_STATES.has(state)) return "execution";
    return "";
  }

  function makeStoreStages(facts, currentStage, blocked) {
    const definitions = [
      ["agent", "本地 Agent", facts.online, "连接本地经营能力"],
      ["store", "打开抖店", facts.storeConfirmed, "自动采用当前抖店页面"],
      ["data", "经营数据", facts.dataReady, "获取新鲜店铺快照"],
      ["task", "今日任务", facts.taskCount === 0 && facts.dataReady, "执行今天最值得处理的事项"],
      ["result", "结果复盘", facts.complete, "沉淀已验证的经营结果"],
    ];
    let stopped = false;
    return definitions.map(([id, label, done, summary]) => {
      let status = "pending";
      if (!stopped && done) status = "done";
      else if (!stopped && id === currentStage) {
        status = blocked ? "blocked" : "current";
        stopped = true;
      } else if (!stopped && !done) {
        status = "pending";
        stopped = true;
      }
      return stage(id, label, status, summary);
    });
  }

  function makeAdsStages(facts, currentStage, blocked) {
    const definitions = [
      ["agent", "本地 Agent", facts.online, "连接本地经营能力"],
      ["store", "打开抖店", facts.storeConfirmed, "自动采用当前抖店页面"],
      ["account", "千川账户", Boolean(facts.selectedAccountKey), "锁定唯一投放账户"],
      ["plans", "计划快照", facts.plans.fresh, "读取当前账户的新鲜计划"],
      ["identity", "计划身份", facts.plans.identityReady, "核对账户、计划 ID 与推广模式"],
      ["boundaries", "投放边界", facts.boundariesReady, "确认预算、止损和人工授权边界"],
      ["execution", "受控执行", facts.executionDone, "执行单笔授权动作"],
      ["readback", "结果回读", facts.complete, "验证动作是否真正有效"],
    ];
    let stopped = false;
    return definitions.map(([id, label, done, summary]) => {
      let status = "pending";
      if (!stopped && done) status = "done";
      else if (!stopped && id === currentStage) {
        status = blocked ? "blocked" : "current";
        stopped = true;
      } else if (!stopped && !done) {
        status = "pending";
        stopped = true;
      }
      return stage(id, label, status, summary);
    });
  }

  function deriveJourney(inputs = {}) {
    const source = object(inputs);
    const lane = normalizeLane(source.lane);
    const onboarding = object(source.onboarding);
    const connectionGuide = object(source.connectionGuide || source.connection_guide);
    const operationContext = object(source.operationContext || source.operation_context);
    const ops = object(source.ops);
    const scan = object(source.scan);
    const capability = object(source.capability);
    const preflight = object(source.preflight);
    const planConsole = object(source.planConsole || source.plan_console);
    const effectiveness = object(source.effectiveness);
    const online = source.online !== false && source.agent_online !== false;
    const storeConfirmed = onboarding.store_confirmed === true || connectionGuide.store_confirmed === true;
    const selectedAccountKey = resolveSelectedAccount(source, onboarding, operationContext, planConsole, preflight);
    // Scan/onboarding fields are historical evidence. Only the connection
    // guide's operational contract may declare data usable right now.
    const operational = operationalTruth(connectionGuide);
    const dataReady = operational.coreDataFresh;
    const tasks = taskCount(ops);
    const task = firstTask(ops);
    const effectCount = evaluatedCount(effectiveness);
    const plans = planFacts(planConsole, selectedAccountKey, capability);
    const boundaryReady = boundariesReady(preflight, capability, operationContext);
    const active = activeJourney(preflight, effectiveness);
    const conflict = identityConflict(source, onboarding, connectionGuide, operationContext, planConsole, selectedAccountKey, preflight);
    const conflicts = conflictMessages(source, onboarding, connectionGuide, operationContext, planConsole, selectedAccountKey, preflight);

    const blockers = [];
    const evidenceItems = [
      evidence("agent", "本地 Agent", online ? "ok" : "blocked", online ? "已连接" : "未连接"),
      evidence("store", "抖店页面", storeConfirmed ? "ok" : "missing", storeConfirmed ? text(onboarding.store_name || connectionGuide.store_name || "已准备") : "尚未打开"),
      evidence("data", "经营数据", dataReady ? "ok" : operational.dataState, dataReady ? "可用于判断" : dataProblem(operational)),
    ];
    if (lane === "ads") {
      evidenceItems.push(
        evidence("account", "千川账户", selectedAccountKey ? "ok" : "missing", selectedAccountKey || "未选择"),
        evidence("plans", "计划快照", plans.fresh ? "ok" : plans.total ? "stale" : "missing", plans.fresh ? `${plans.total} 个新鲜计划` : plans.total ? `${plans.staleRows || plans.total} 个计划需要刷新` : "未读取"),
        evidence("identity", "计划身份", plans.identityReady ? "ok" : "blocked", plans.identityReady ? "核对通过" : "未通过账户与计划身份校验"),
        evidence("boundaries", "预算与止损边界", boundaryReady ? "ok" : "blocked", boundaryReady ? "已确认" : "尚未确认"),
      );
    }
    evidenceItems.push(
      evidence("tasks", "今日待办", tasks ? "active" : "ok", tasks ? `${tasks} 项待处理` : "无待办"),
      evidence("results", "已验证结果", effectCount ? "ok" : "info", effectCount ? `${effectCount} 项` : "暂无"),
    );

    let state = "complete";
    let status = "complete";
    let currentStage = "result";
    let title = lane === "ads" ? "本轮投放经营已完成" : "本轮店铺经营已完成";
    let detail = effectCount ? `已有 ${effectCount} 项结果完成验证。` : "当前没有必须处理的事项，可查看经营结果。";
    let primaryAction = action("review_results", "navigate", "查看经营结果", lane === "ads" ? "promotion-operation-log" : "today-task-center", "复盘已完成动作并沉淀有效经验");
    let blocked = false;

    // The order below is the product contract. Do not reorder without a migration.
    if (!online) {
      state = "offline";
      status = "blocked";
      currentStage = "agent";
      title = "本地 Agent 未连接";
      detail = "先恢复本地 Agent，巡店、任务和投放动作才有可信数据来源。";
      blockers.push(blocker("agent_offline", "本地 Agent 未连接", "simple-start"));
      primaryAction = action("repair_agent", "repair_agent", "修复本地 Agent", "simple-start", detail);
      blocked = true;
    } else if (conflict) {
      state = "identity_blocked";
      status = "blocked";
      currentStage = "store";
      title = "当前页面已切换";
      detail = "为避免混用数据，本次操作已暂停。请回到要经营的页面后重新开始。";
      blockers.push(blocker("identity_conflict", detail, "simple-start"));
      primaryAction = action("start_store_scan", "scan", "重新打开并巡店", "scan-card", detail);
      blocked = true;
    } else if (active) {
      state = active === "readback" ? "readback_in_progress" : "authorization_in_progress";
      status = "active";
      currentStage = active === "readback" ? "readback" : "execution";
      title = active === "readback" ? "正在等待结果回读" : "受控投放正在处理中";
      detail = active === "readback" ? "保持当前授权会话，完成结果读取和效果核验。" : "当前存在进行中的授权或执行会话，请先完成它。";
      primaryAction = action(active === "readback" ? "continue_readback" : "continue_execution", "navigate", active === "readback" ? "继续结果回读" : "继续受控执行", active === "readback" ? "promotion-operation-log" : "automation-section", detail);
    } else if (!storeConfirmed) {
      state = "store_unconfirmed";
      status = "blocked";
      currentStage = "store";
      title = "打开抖店，直接开始巡店";
      detail = "系统会自动采用当前抖店页面，不需要识别、绑定或手工选择。";
      blockers.push(blocker("store_unconfirmed", "尚未打开可用的抖店页面", "scan-card"));
      primaryAction = {
        ...action("start_store_scan", "scan", "打开抖店并开始", "scan-card", detail),
        pageIds: ["overview", "orders", "products", "shelf"],
      };
    } else if (!dataReady) {
      state = "data_required";
      status = "attention";
      currentStage = "data";
      title = "先刷新经营数据";
      detail = dataProblem(operational);
      blockers.push(blocker("store_data_unavailable", detail, "scan-card"));
      if (["stale", "failed"].includes(operational.dataState)) {
        primaryAction = {
          ...action(
            "refresh_core_data",
            "scan",
            operational.refreshPageIds.length ? `刷新 ${operational.refreshPageIds.length} 个核心经营页` : "刷新核心经营数据",
            "scan-card",
            detail,
          ),
          pageIds: operational.refreshPageIds,
        };
      } else {
        primaryAction = {
          ...action("quick_scan", "scan", "同步核心经营数据", "scan-card", detail),
          pageIds: ["overview", "orders", "products", "shelf"],
        };
      }
    } else if (lane === "ads" && !selectedAccountKey) {
      state = "ads_account_blocked";
      status = "blocked";
      currentStage = "account";
      title = "先选择唯一千川账户";
      detail = "投放数据、授权会话和回读结果必须绑定到同一个账户。";
      blockers.push(blocker("ads_account_missing", "尚未锁定当前千川账户", "promotion-plan-center"));
      primaryAction = action("select_ads_account", "navigate", "选择千川账户", "promotion-plan-center", detail);
      blocked = true;
    } else if (lane === "ads" && !plans.fresh) {
      state = "ads_plan_data_required";
      status = "attention";
      currentStage = "plans";
      title = plans.total ? "计划数据已经过期" : "还没有读取千川计划";
      detail = plans.total ? "重新同步当前账户计划，避免基于旧预算和旧状态执行。" : "先读取当前账户计划，系统才能判断哪些动作可执行。";
      blockers.push(blocker("ads_plans_unavailable", detail, "promotion-plan-center"));
      primaryAction = action("sync_ads_plans", "sync_ads", "同步千川计划", "promotion-plan-center", detail);
    } else if (lane === "ads" && !plans.identityReady) {
      state = "ads_plan_identity_blocked";
      status = "blocked";
      currentStage = "identity";
      title = "计划身份校验未通过";
      detail = plans.identityBlockers[0] || "需要补齐账户、计划 ID、推广模式与数据质量证据。";
      (plans.identityBlockers.length ? plans.identityBlockers : [detail]).forEach((message) => blockers.push(blocker("ads_plan_identity_missing", message, "promotion-plan-center")));
      primaryAction = action("repair_plan_identity", "navigate", "补齐计划身份", "promotion-plan-center", detail);
      blocked = true;
    } else if (lane === "ads" && !boundaryReady) {
      state = "ads_boundary_blocked";
      status = "blocked";
      currentStage = "boundaries";
      title = "先确认预算与止损边界";
      detail = "缺少预算上限、止损条件或人工授权边界时，不允许进入投放执行。";
      blockers.push(blocker("ads_boundary_missing", detail, "automation-section"));
      primaryAction = action("configure_boundaries", "navigate", "设置投放边界", "automation-section", detail);
      blocked = true;
    } else if (tasks > 0) {
      state = "today_task_ready";
      status = "ready";
      currentStage = lane === "ads" ? "execution" : "task";
      const taskTitle = text(object(task).title || object(task).name);
      title = taskTitle || `今天有 ${tasks} 项任务待处理`;
      detail = text(object(task).action || object(task).next_step) || "先处理优先级最高且证据完整的一项任务。";
      primaryAction = action("start_today_task", "navigate", taskTitle ? "开始处理" : "查看今日任务", "today-task-center", detail);
    }

    const facts = {
      online,
      storeConfirmed,
      dataReady,
      taskCount: tasks,
      complete: state === "complete",
      selectedAccountKey,
      plans,
      boundariesReady: boundaryReady,
      executionDone: state === "complete" || state === "readback_in_progress" || effectCount > 0,
    };
    const stages = lane === "ads"
      ? makeAdsStages(facts, currentStage, blocked)
      : makeStoreStages(facts, currentStage, blocked);

    return {
      schemaVersion: 1,
      lane,
      state,
      status,
      dataState: operational.dataState,
      currentStage,
      title,
      detail,
      primaryAction,
      stages,
      blockers,
      evidence: evidenceItems,
      scope: {
        storeId: text(onboarding.store_id || connectionGuide.store_id || operationContext.store_id),
        accountKey: lane === "ads" ? selectedAccountKey : "",
      },
    };
  }

  return {
    ACTIVE_AUTH_STATES,
    ACTIVE_READBACK_STATES,
    normalizeLane,
    operationalTruth,
    deriveJourney,
  };
});
