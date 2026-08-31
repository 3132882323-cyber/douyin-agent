(function initSimpleExperience(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.DianSimpleExperience = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function simpleExperienceFactory() {
  "use strict";

  function normalizeMode(value) {
    return String(value || "").trim().toLowerCase() === "professional" ? "professional" : "simple";
  }

  function completedStep(onboarding, id) {
    const steps = Array.isArray(onboarding?.steps) ? onboarding.steps : [];
    return steps.some((step) => step?.id === id && step.complete === true);
  }

  const CORE_PAGE_IDS = new Set(["overview", "orders", "products", "shelf"]);

  function pageIds(value) {
    return [...new Set((Array.isArray(value) ? value : [])
      .map((item) => String(item || "").trim())
      .filter((item) => CORE_PAGE_IDS.has(item)))];
  }

  function operationalTruth(connectionGuide = {}) {
    const operational = connectionGuide?.operational && typeof connectionGuide.operational === "object"
      ? connectionGuide.operational
      : {};
    const state = String(operational.state || "setup_required").trim().toLowerCase();
    const connectedLevel = String(operational.connected_level || connectionGuide?.level || "L0").trim().toUpperCase();
    const refreshPageIds = pageIds(
      operational.refresh_page_ids
      || connectionGuide?.next_upgrade?.page_ids,
    );
    const coreDataFresh = operational.core_data_fresh === true;
    let dataState = "never";
    if (coreDataFresh) dataState = "fresh";
    else if (["loading", "checking", "refreshing"].includes(state)) dataState = "loading";
    else if (["failed", "error"].includes(state)) dataState = "failed";
    else if (["refresh_required", "stale", "expired"].includes(state)
      || connectionGuide?.next_upgrade?.id === "refresh_core_data"
      || /^L[2-9]$/.test(connectedLevel)) dataState = "stale";
    return {
      state,
      connectedLevel,
      dataState,
      coreDataFresh,
      executionReviewReady: operational.execution_review_ready === true,
      refreshPageIds,
    };
  }

  function onboardingEntryTarget({ mode = "simple", route = "", journey = {} } = {}) {
    if (String(route || "").trim()) return "route";
    return normalizeMode(mode) === "simple" && journey?.complete !== true
      ? "simple-start"
      : "today-task-center";
  }

  function deriveEmptyState(input = {}) {
    const hasTasks = input.has_tasks === true || Number(input.task_count || 0) > 0;
    if (hasTasks) return { id: "actionable", title: "存在待处理任务", action: "view_tasks" };
    const dataState = String(input.data_state || "never").trim().toLowerCase();
    if (input.loading === true || dataState === "loading") {
      return { id: "loading", title: "正在生成今日任务", detail: "正在核对店铺、数据时间和诊断结果。", action: "none" };
    }
    if (input.failed === true || dataState === "failed") {
      return { id: "failed", title: "最近一次数据读取失败", detail: "失败不代表经营正常；请重试失败页面后再生成任务。", action: "refresh_core_data" };
    }
    if (dataState === "stale") {
      return { id: "stale", title: "经营数据已经过期", detail: "旧数据不能证明当前没有问题；请先刷新核心经营页。", action: "refresh_core_data" };
    }
    if (dataState === "never") {
      return { id: "never", title: "还没有生成经营任务", detail: "先完成首次数据同步，系统才会判断今天需要处理什么。", action: "quick_scan" };
    }
    if (input.clean === true) {
      return { id: "clean", title: "本轮检查未发现异常", detail: "数据新鲜且检查完成，可以等待下一次巡店。", action: "view_results" };
    }
    return { id: "no_actionable_task", title: "当前没有可执行任务", detail: "数据已刷新，但没有证据充分、值得立即处理的动作。", action: "view_results" };
  }

  function deriveJourney(onboarding = {}, connectionGuide = {}) {
    const storeReady = onboarding.store_confirmed === true || completedStep(onboarding, "store");
    const operational = operationalTruth(connectionGuide);
    // Historical onboarding milestones explain how far the user once got. Only
    // connectionGuide.operational may say whether data is usable right now.
    const dataReady = storeReady && operational.coreDataFresh;
    const actionReady = dataReady && (
      onboarding.status === "completed"
      || completedStep(onboarding, "evidence")
      || completedStep(onboarding, "first_task") && onboarding.first_task?.status && onboarding.first_task.status !== "todo"
    );
    const steps = [
      { id: "open_store", label: "打开抖店", detail: "保持抖店后台登录", complete: storeReady },
      { id: "auto_confirm", label: "自动确认", detail: "系统自动采用当前页面", complete: storeReady },
      { id: "scan", label: "开始巡店", detail: "读取关键数据并给出建议", complete: dataReady },
    ];
    const currentIndex = Math.min(steps.findIndex((step) => !step.complete), steps.length - 1);
    const safeIndex = currentIndex < 0 ? steps.length - 1 : currentIndex;
    let action = {
      id: "view_today",
      label: "查看今天先做什么",
      title: "准备完成，开始今天的经营",
      detail: "系统会按风险和收益排序，你只需从第一件事开始。",
    };
    if (!storeReady) {
      action = {
        id: "start_store_scan",
        label: "打开抖店并开始",
        title: "打开抖店，就能自动开始巡店",
        detail: "系统会自动确认当前页面并开始读取，不需要识别、绑定或填写账号密码。",
      };
    } else if (!dataReady && ["stale", "failed"].includes(operational.dataState)) {
      const failed = operational.dataState === "failed";
      action = {
        id: "refresh_core_data",
        label: operational.refreshPageIds.length
          ? `刷新 ${operational.refreshPageIds.length} 个核心经营页`
          : "刷新核心经营数据",
        title: failed ? "最近一次核心数据读取失败" : "连接仍然有效，但经营数据已经过期",
        detail: operational.refreshPageIds.length
          ? failed
            ? "只重试读取失败或缺失的页面，不会扩大采集范围。"
            : "只刷新已确认过期或缺失的页面，不会扩大采集范围。"
          : "先查看待刷新页面；未确认范围前不会启动全店扫描。",
        page_ids: operational.refreshPageIds,
      };
    } else if (!dataReady && operational.dataState === "loading") {
      action = {
        id: "none",
        label: "正在核对数据",
        title: "正在核对核心经营数据",
        detail: "完成前不会重复启动扫描，也不会把空结果当成经营正常。",
        page_ids: [],
      };
    } else if (!dataReady) {
      action = {
        id: "quick_scan",
        label: "开始巡店",
        title: "当前页面已准备好",
        detail: "自动读取经营、订单、商品和商城关键页面，通常需要 3–5 分钟。",
      };
    } else if (!actionReady) {
      action = {
        id: "view_first_task",
        label: "查看第一条建议",
        title: "数据已经准备好",
        detail: "现在只看问题、建议动作和完成标准，不需要先学习全部功能。",
      };
    }
    return {
      complete: steps.every((step) => step.complete),
      completed: steps.filter((step) => step.complete).length,
      total: steps.length,
      percent: Math.round(steps.filter((step) => step.complete).length / steps.length * 100),
      current_index: safeIndex,
      steps: steps.map((step, index) => ({
        ...step,
        current: !steps.every((item) => item.complete) && index === safeIndex,
      })),
      action,
      data_state: operational.dataState,
      operational,
      optional_note: "先完成店铺巡检；需要投放时，再进入“直播与投放”。",
    };
  }

  return {
    normalizeMode,
    operationalTruth,
    onboardingEntryTarget,
    deriveEmptyState,
    deriveJourney,
  };
});
