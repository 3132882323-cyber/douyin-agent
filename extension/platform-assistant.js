/** 店策页内助手：只在用户明确点击后同步当前抖店/千川页面。 */
(function exposePlatformAssistant(root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.DianPlatformAssistant = api;
  if (root?.document && root?.chrome?.runtime?.id) api.autoMount(root).catch(() => undefined);
})(typeof globalThis !== "undefined" ? globalThis : this, function createPlatformAssistant() {
  "use strict";

  const HOST_ATTRIBUTE = "data-dian-agent-platform-assistant";
  const DISMISSED_KEY = "platformAssistantDismissedV2";
  const LEGACY_DISMISSED_KEY = "platformAssistantDismissedV1";
  const COLLAPSED_KEY = "platformAssistantCollapsedV1";
  const PREFERENCE_LISTENER_MARK = "__DianPlatformAssistantPreferenceListenerV2";
  const REQUEST_TIMEOUT_MS = 8_000;
  const SOURCE_LABELS = Object.freeze({ doudian: "抖店", qianchuan: "千川" });
  const PAGE_LABELS = Object.freeze({
    overview: "经营概览",
    orders: "订单管理",
    products: "商品管理",
    inventory: "库存管理",
    refunds: "售后管理",
    reviews: "评价管理",
    shelf: "商城运营",
    live: "直播经营",
    short_video: "短视频素材",
    image_text: "图文素材",
    recommend_card: "商品卡",
    search: "搜索经营",
    funds: "资金账户",
    campaigns: "商品计划",
    materials: "视频素材",
    video_library: "视频素材",
    live_dashboard: "直播大屏",
    qianchuan_campaigns: "商品计划",
    qianchuan_live: "直播计划",
    qianchuan_report: "投放报表",
    qianchuan_video_library: "视频素材",
    qianchuan_live_dashboard: "直播大屏",
    unknown: "当前页面",
  });

  function sourceForLocation(locationValue = {}) {
    const host = String(locationValue.hostname || "").toLowerCase();
    if (host === "fxg.jinritemai.com") return "doudian";
    if (["qianchuan.jinritemai.com", "buyin.jinritemai.com"].includes(host)) return "qianchuan";
    return "";
  }

  function preferenceKey(prefix, source) {
    const normalized = ["doudian", "qianchuan"].includes(String(source || "")) ? String(source) : "";
    return normalized ? `${prefix}.${normalized}` : prefix;
  }

  function dismissedFromStorage(record = {}, source = "") {
    const scopedKey = preferenceKey(DISMISSED_KEY, source);
    if (Object.hasOwn(record || {}, scopedKey)) return record?.[scopedKey] === true;
    return record?.[LEGACY_DISMISSED_KEY] === true;
  }

  async function readPreferences(root, source) {
    try {
      const dismissedKey = preferenceKey(DISMISSED_KEY, source);
      const collapsedKey = preferenceKey(COLLAPSED_KEY, source);
      const record = await root?.chrome?.storage?.local?.get?.([dismissedKey, collapsedKey, LEGACY_DISMISSED_KEY]);
      return {
        dismissed: dismissedFromStorage(record, source),
        collapsed: record?.[collapsedKey] === true,
        collapsed_set: Object.hasOwn(record || {}, collapsedKey),
      };
    } catch (_) {
      return { dismissed: false, collapsed: false, collapsed_set: false };
    }
  }

  async function writeDismissed(root, source, dismissed) {
    await root?.chrome?.storage?.local?.set?.({ [preferenceKey(DISMISSED_KEY, source)]: dismissed === true });
  }

  async function writeCollapsed(root, source, collapsed) {
    await root?.chrome?.storage?.local?.set?.({ [preferenceKey(COLLAPSED_KEY, source)]: collapsed === true });
  }

  function compactVersion(value = "") {
    const version = String(value || "").trim();
    return /^\d+(?:\.\d+){2,3}$/.test(version) ? version : "";
  }

  function shortExtensionId(value = "") {
    const extensionId = String(value || "").trim().toLowerCase();
    return /^[a-p]{32}$/.test(extensionId) ? `${extensionId.slice(0, 6)}…${extensionId.slice(-4)}` : "";
  }

  function collectorVersion(root, source) {
    const key = source === "doudian"
      ? "__DianAgentDoudianRuntimeVersion"
      : "__DianAgentQianchuanRuntimeVersion";
    return compactVersion(root?.[key]);
  }

  function isRecoveryError(error) {
    return /extension context invalidated|receiving end does not exist|message port closed/i.test(
      String(error?.message || error || ""),
    );
  }

  function isTrustedGesture(event) {
    return event?.isTrusted === true;
  }

  function clockLabel(timestamp, now = Date.now()) {
    const value = Number(timestamp || 0);
    if (!Number.isFinite(value) || value <= 0) return "尚未同步";
    const elapsed = Math.max(0, Number(now || Date.now()) - value);
    if (elapsed < 60_000) return "刚刚同步";
    if (elapsed < 60 * 60_000) return `${Math.max(1, Math.floor(elapsed / 60_000))} 分钟前同步`;
    return new Date(value).toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  }

  function pageLabel(pageType = "") {
    return PAGE_LABELS[String(pageType || "").trim().toLowerCase()] || "当前页面";
  }

  async function runtimeMessage(runtime, message, timeoutMs = REQUEST_TIMEOUT_MS) {
    let timer = null;
    try {
      return await Promise.race([
        runtime.sendMessage(message),
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error("本地响应超过 8 秒，请重试")), timeoutMs);
        }),
      ]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  function buildMarkup(sourceLabel) {
    return `
      <style>
        :host { all: initial; }
        * { box-sizing: border-box; }
        .dock { width: min(336px, calc(100vw - 32px)); color: #17202f; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
        .card { display: flex; max-height: calc(100vh - 32px); overflow: hidden; flex-direction: column; border: 1px solid #c7d7fe; border-radius: 16px; background: rgba(255,255,255,.98); box-shadow: 0 18px 45px rgba(15,23,42,.20); backdrop-filter: blur(12px); }
        .head { display: flex; align-items: center; gap: 10px; padding: 13px 14px; border-bottom: 1px solid #e6ecf8; background: linear-gradient(135deg,#f8fbff,#eef4ff); }
        .mark { display: grid; place-items: center; width: 36px; height: 36px; flex: 0 0 auto; border-radius: 11px; background: linear-gradient(145deg,#1d4ed8,#3b82f6); color: #fff; font-size: 17px; font-weight: 900; box-shadow: 0 7px 18px rgba(37,99,235,.24); }
        .heading { min-width: 0; flex: 1; }
        .heading strong { display: block; color: #102a56; font-size: 15px; line-height: 1.3; }
        .heading span { display: block; margin-top: 2px; color: #667085; font-size: 12px; line-height: 1.35; }
        button { font: inherit; }
        .head-actions { display: flex; align-items: center; gap: 3px; }
        .icon-action { display: grid; place-items: center; width: 44px; height: 44px; padding: 0; border: 0; border-radius: 11px; color: #475467; background: transparent; cursor: pointer; touch-action: manipulation; font-size: 18px; line-height: 1; }
        .icon-action:hover { background: #e5edff; }
        .close:hover, .mini-close:hover { color: #b42318; background: #fff1f0; }
        button:focus-visible { outline: 3px solid rgba(37,99,235,.28); outline-offset: 2px; }
        .body { overflow: auto; padding: 14px; }
        .state-row { display: flex; align-items: center; gap: 8px; min-height: 24px; }
        .dot { width: 9px; height: 9px; border-radius: 999px; background: #f79009; box-shadow: 0 0 0 4px #fff4e6; }
        .state-row[data-state="ok"] .dot { background: #12b76a; box-shadow: 0 0 0 4px #ecfdf3; }
        .state-row[data-state="error"] .dot { background: #f04438; box-shadow: 0 0 0 4px #fef3f2; }
        .state-row strong { flex: 1; font-size: 14px; line-height: 1.4; }
        .version { color: #667085; font-size: 11px; }
        .notice { margin: 9px 0 12px; min-height: 36px; color: #667085; font-size: 12px; line-height: 1.55; }
        .notice.error { color: #b42318; }
        .notice.success { color: #067647; font-weight: 700; }
        .actions { display: grid; grid-template-columns: 1fr auto; gap: 8px; }
        .primary, .secondary, .refresh { min-height: 40px; border-radius: 10px; cursor: pointer; font-weight: 800; }
        .primary { border: 0; padding: 0 16px; color: #fff; background: #2563eb; font-size: 14px; box-shadow: 0 7px 15px rgba(37,99,235,.20); }
        .primary:hover { background: #1d4ed8; }
        .primary:disabled { cursor: wait; opacity: .65; }
        .secondary { border: 1px solid #d0d5dd; padding: 0 12px; color: #344054; background: #fff; font-size: 12px; }
        .secondary:hover { border-color: #84adff; color: #175cd3; }
        .runtime-warning { display: flex; align-items: center; gap: 8px; margin-top: 11px; padding: 9px 10px; border: 1px solid #fedf89; border-radius: 9px; background: #fffaeb; color: #93370d; font-size: 11px; line-height: 1.45; }
        .runtime-warning[hidden] { display: none; }
        .runtime-warning span { flex: 1; }
        .refresh { min-height: 30px; border: 1px solid #fdb022; padding: 0 9px; color: #93370d; background: #fff; font-size: 11px; white-space: nowrap; }
        .privacy { margin: 10px 0 0; color: #667085; font-size: 11px; line-height: 1.5; }
        .mini { display: none; align-items: center; gap: 2px; margin-left: auto; width: max-content; border: 1px solid #b2ccff; border-radius: 999px; padding: 5px; color: #175cd3; background: #fff; box-shadow: 0 12px 30px rgba(15,23,42,.18); font: 800 13px/1 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif; }
        .mini-open { display: flex; align-items: center; gap: 8px; min-height: 44px; padding: 0 8px 0 0; border: 0; color: #175cd3; background: transparent; cursor: pointer; touch-action: manipulation; font-weight: 800; }
        .mini-open b { display: grid; place-items: center; width: 36px; height: 36px; border-radius: 999px; color: #fff; background: #2563eb; }
        .mini-close { display: grid; place-items: center; width: 44px; height: 44px; padding: 0; border: 0; border-radius: 999px; color: #667085; background: transparent; cursor: pointer; touch-action: manipulation; font-size: 18px; line-height: 1; }
        .dock.collapsed .card { display: none; }
        .dock.collapsed .mini { display: flex; }
        @media (max-width: 720px) { .dock { width: min(320px, calc(100vw - 24px)); } }
        @media (prefers-reduced-motion: reduce) { * { scroll-behavior: auto !important; transition: none !important; animation: none !important; } }
      </style>
      <div class="dock">
        <aside class="card" role="region" aria-label="店策页内助手">
          <header class="head">
            <div class="mark" aria-hidden="true">策</div>
            <div class="heading"><strong>店策页内助手</strong><span>${sourceLabel} · 连接、同步、看结果</span></div>
            <div class="head-actions">
              <button class="collapse icon-action" type="button" aria-label="收起店策页内助手" aria-expanded="true" title="收起">−</button>
              <button class="close icon-action" type="button" aria-label="关闭店策页内助手" title="关闭悬浮助手">×</button>
            </div>
          </header>
          <div class="body">
            <div class="state-row" data-state="pending"><span class="dot"></span><strong class="state-title">正在检查本地 Agent</strong><span class="version"></span></div>
            <p class="notice" role="status" aria-live="polite">连接状态和数据同步是两件事；同步成功后才会生成新经营数据。</p>
            <div class="actions">
              <button class="primary" type="button">同步当前页</button>
              <button class="secondary" type="button">打开工作台</button>
            </div>
            <div class="runtime-warning" hidden><span>当前页仍是旧采集组件。请先保存正在编辑的计划或预算，再主动刷新并重新接入。</span><button class="refresh" type="button">刷新并重新接入</button></div>
            <p class="privacy">只在你点击后读取当前页面；不读取 Cookie，数据仅发送至本机 Agent。关闭后可从插件弹窗重新显示。</p>
          </div>
        </aside>
        <div class="mini">
          <button class="mini-open" type="button" aria-label="展开店策页内助手" aria-expanded="false"><b>策</b><span>同步${sourceLabel}</span></button>
          <button class="mini-close" type="button" aria-label="关闭店策页内助手" title="关闭悬浮助手">×</button>
        </div>
      </div>`;
  }

  function applyCollapsedState(host, collapsed) {
    const dock = host?.shadowRoot?.querySelector?.(".dock");
    const collapse = host?.shadowRoot?.querySelector?.(".collapse");
    const miniOpen = host?.shadowRoot?.querySelector?.(".mini-open");
    if (!dock) return;
    dock.classList.toggle("collapsed", collapsed === true);
    collapse?.setAttribute("aria-expanded", String(collapsed !== true));
    miniOpen?.setAttribute("aria-expanded", String(collapsed !== true));
  }

  function restorePageFocusAfterDismissal(root, sourceLabel = "") {
    const documentRef = root?.document;
    if (!documentRef?.documentElement) return;
    const announcer = documentRef.createElement("p");
    announcer.setAttribute("data-dian-agent-platform-assistant-announcer", "true");
    announcer.setAttribute("role", "status");
    announcer.setAttribute("aria-live", "polite");
    announcer.setAttribute("aria-atomic", "true");
    Object.assign(announcer.style, {
      position: "fixed",
      width: "1px",
      height: "1px",
      overflow: "hidden",
      clipPath: "inset(50%)",
      whiteSpace: "nowrap",
    });
    documentRef.documentElement.appendChild(announcer);

    const focusTarget = documentRef.querySelector?.("main, [role='main']") || documentRef.body || documentRef.documentElement;
    const addedTabIndex = focusTarget?.hasAttribute?.("tabindex") === false;
    if (addedTabIndex) focusTarget.setAttribute("tabindex", "-1");
    try {
      focusTarget?.focus?.({ preventScroll: true });
    } catch (_) {
      focusTarget?.focus?.();
    }
    announcer.textContent = `店策${sourceLabel ? ` ${sourceLabel}` : ""}页内助手已关闭，可从浏览器扩展弹窗重新显示。`;
    const schedule = typeof root?.setTimeout === "function" ? root.setTimeout.bind(root) : setTimeout;
    schedule(() => {
      announcer.remove();
      if (addedTabIndex) focusTarget?.removeAttribute?.("tabindex");
    }, 3_000);
  }

  function ensurePreferenceListener(root, source) {
    if (root?.[PREFERENCE_LISTENER_MARK] || !root?.chrome?.storage?.onChanged?.addListener) return;
    const dismissedKey = preferenceKey(DISMISSED_KEY, source);
    const collapsedKey = preferenceKey(COLLAPSED_KEY, source);
    const listener = (changes, areaName) => {
      if (areaName !== "local" || (!changes[dismissedKey] && !changes[collapsedKey] && !changes[LEGACY_DISMISSED_KEY])) return;
      readPreferences(root, source).then((preferences) => {
        const host = root.document?.querySelector?.(`[${HOST_ATTRIBUTE}="true"]`);
        if (preferences.dismissed) {
          host?.remove();
          return;
        }
        if (host) applyCollapsedState(host, preferences.collapsed);
        else mount(root, { collapsed: preferences.collapsed });
      }).catch(() => undefined);
    };
    root.chrome.storage.onChanged.addListener(listener);
    root[PREFERENCE_LISTENER_MARK] = { source, listener };
  }

  async function autoMount(root) {
    const source = sourceForLocation(root?.location || {});
    if (!source) return null;
    ensurePreferenceListener(root, source);
    const preferences = await readPreferences(root, source);
    if (preferences.dismissed) {
      root.document?.querySelector?.(`[${HOST_ATTRIBUTE}="true"]`)?.remove();
      return null;
    }
    return mount(root, {
      collapsed: preferences.collapsed || !preferences.collapsed_set,
    });
  }

  function mount(root, options = {}) {
    const documentRef = root.document;
    const runtime = root.chrome?.runtime;
    const source = sourceForLocation(root.location || {});
    if (!documentRef?.documentElement || !runtime?.id || !source) return null;

    const runtimeVersion = compactVersion(runtime.getManifest?.().version);
    const existing = documentRef.querySelector(`[${HOST_ATTRIBUTE}="true"]`);
    if (existing?.dataset?.runtimeVersion === runtimeVersion) {
      applyCollapsedState(existing, options.collapsed === true);
      return existing;
    }
    if (existing) existing.remove();

    const host = documentRef.createElement("div");
    host.setAttribute(HOST_ATTRIBUTE, "true");
    host.dataset.runtimeVersion = runtimeVersion;
    host.dataset.extensionId = runtime.id;
    Object.assign(host.style, {
      all: "initial",
      position: "fixed",
      right: "max(16px, env(safe-area-inset-right))",
      bottom: "max(16px, env(safe-area-inset-bottom))",
      zIndex: "900",
      display: "block",
    });
    const shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML = buildMarkup(SOURCE_LABELS[source]);
    documentRef.documentElement.appendChild(host);
    applyCollapsedState(host, options.collapsed === true);

    const dock = shadow.querySelector(".dock");
    const stateRow = shadow.querySelector(".state-row");
    const stateTitle = shadow.querySelector(".state-title");
    const version = shadow.querySelector(".version");
    const notice = shadow.querySelector(".notice");
    const primary = shadow.querySelector(".primary");
    const secondary = shadow.querySelector(".secondary");
    const collapse = shadow.querySelector(".collapse");
    const close = shadow.querySelector(".close");
    const miniOpen = shadow.querySelector(".mini-open");
    const miniClose = shadow.querySelector(".mini-close");
    const warning = shadow.querySelector(".runtime-warning");
    const warningCopy = shadow.querySelector(".runtime-warning span");
    const refresh = shadow.querySelector(".refresh");
    const pageCollectorVersion = collectorVersion(root, source);
    let requiresRefresh = !(pageCollectorVersion && pageCollectorVersion === runtimeVersion);

    function setNotice(text, tone = "") {
      notice.textContent = String(text || "");
      notice.className = `notice${tone ? ` ${tone}` : ""}`;
    }

    function setConnection(state, title, detail = "") {
      stateRow.dataset.state = state;
      stateTitle.textContent = title;
      version.textContent = detail;
    }

    function recoverInvalidContext(error) {
      if (!isRecoveryError(error)) return false;
      setConnection("error", "插件刚刚完成更新", "页面需刷新");
      setNotice("当前页面不会自动刷新，避免丢失尚未保存的计划或预算。请保存后再手动刷新。", "error");
      warning.hidden = false;
      warningCopy.textContent = "插件已更新。请先保存当前编辑内容，再点击刷新并重新接入。";
      refresh.disabled = false;
      refresh.textContent = "刷新并重新接入";
      primary.disabled = true;
      requiresRefresh = true;
      return true;
    }

    async function loadStatus() {
      try {
        const response = await runtimeMessage(runtime, {
          type: "platform-assistant-status",
          source,
          page_runtime_version: pageCollectorVersion,
        });
        if (!response?.ok) throw new Error(response?.error || "连接状态读取失败");
        const agent = response.agent || {};
        const idLabel = shortExtensionId(agent.extension_id || runtime.id);
        const runtimeLabel = [agent.runtime_version ? `v${agent.runtime_version}` : "", idLabel].filter(Boolean).join(" · ");
        if (agent.connected === true) {
          const latestAttemptFailed = response.last_sync_status === "error"
            && Number(response.last_sync_attempt_at || 0) > Number(response.last_sync_at || 0);
          if (latestAttemptFailed) {
            setConnection("pending", "上次同步未完成", runtimeLabel);
            const previous = Number(response.last_sync_at || 0) > 0 ? `；上次成功 ${clockLabel(response.last_sync_at)}` : "";
            setNotice(`${response.last_sync_message || "请确认当前页面和账户后重试"}${previous}`, "error");
          } else {
            setConnection("ok", "本地 Agent 已连接", runtimeLabel);
            setNotice(`${clockLabel(response.last_sync_at)}；点击“同步当前页”才会读取并保存新数据。`);
          }
        } else if (agent.liveness_ok === true) {
          setConnection("pending", "Agent 已运行，插件连接待恢复", runtimeLabel);
          setNotice(agent.message || "请点击同步重试；仍失败时再打开工作台查看修复原因。", "error");
        } else {
          setConnection("error", "本地 Agent 未连接", "");
          setNotice(agent.message || "请先从 Windows 开始菜单运行 Repair Dian Agent。", "error");
        }
      } catch (error) {
        if (recoverInvalidContext(error)) return;
        setConnection("error", "扩展后台暂不可用", "");
        setNotice(error.message || String(error), "error");
      }
    }

    async function syncCurrentPage(event) {
      if (!isTrustedGesture(event)) return;
      const previousLabel = primary.textContent;
      primary.disabled = true;
      primary.textContent = "正在读取并校验…";
      setNotice("正在读取当前页面，并交给本机 Agent 做身份与数据质量校验。");
      try {
        const response = await runtimeMessage(runtime, { type: "platform-assistant-sync", source }, 35_000);
        if (!response?.ok) throw new Error(response?.error || "当前页面同步失败");
        const result = response.result || {};
        setConnection("ok", "当前页同步成功", runtimeVersion ? `v${runtimeVersion}` : "");
        setNotice(`${pageLabel(result.page_type)}已写入本地经营数据 · ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`, "success");
        primary.textContent = "再次同步";
      } catch (error) {
        if (recoverInvalidContext(error)) return;
        setConnection("error", "当前页同步未完成", runtimeVersion ? `v${runtimeVersion}` : "");
        setNotice(error.message || String(error), "error");
        primary.textContent = previousLabel;
      } finally {
        primary.disabled = requiresRefresh;
      }
    }

    async function openWorkbench(event) {
      if (!isTrustedGesture(event)) return;
      secondary.disabled = true;
      try {
        const response = await runtimeMessage(runtime, { type: "platform-assistant-open-workbench", source });
        if (!response?.ok) throw new Error(response?.error || "工作台打开失败");
      } catch (error) {
        if (!recoverInvalidContext(error)) setNotice(error.message || String(error), "error");
      } finally {
        secondary.disabled = false;
      }
    }

    function dismiss(event) {
      if (!isTrustedGesture(event)) return;
      host.hidden = true;
      restorePageFocusAfterDismissal(root, SOURCE_LABELS[source]);
      writeDismissed(root, source, true).catch(() => undefined).finally(() => host.remove());
    }

    collapse.addEventListener("click", (event) => {
      if (!isTrustedGesture(event)) return;
      applyCollapsedState(host, true);
      writeCollapsed(root, source, true).catch(() => undefined);
      miniOpen.focus();
    });
    miniOpen.addEventListener("click", (event) => {
      if (!isTrustedGesture(event)) return;
      applyCollapsedState(host, false);
      writeCollapsed(root, source, false).catch(() => undefined);
      collapse.focus();
    });
    close.addEventListener("click", dismiss);
    miniClose.addEventListener("click", dismiss);
    primary.addEventListener("click", syncCurrentPage);
    secondary.addEventListener("click", openWorkbench);
    refresh.addEventListener("click", (event) => {
      if (!isTrustedGesture(event)) return;
      refresh.disabled = true;
      refresh.textContent = "正在刷新";
      root.location.reload();
    });
    shadow.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || dock.classList.contains("collapsed")) return;
      event.preventDefault();
      applyCollapsedState(host, true);
      writeCollapsed(root, source, true).catch(() => undefined);
      miniOpen.focus();
    });

    warning.hidden = !requiresRefresh;
    if (requiresRefresh) primary.disabled = true;
    loadStatus();
    return host;
  }

  return Object.freeze({
    HOST_ATTRIBUTE,
    DISMISSED_KEY,
    LEGACY_DISMISSED_KEY,
    COLLAPSED_KEY,
    REQUEST_TIMEOUT_MS,
    sourceForLocation,
    preferenceKey,
    dismissedFromStorage,
    compactVersion,
    shortExtensionId,
    isRecoveryError,
    isTrustedGesture,
    clockLabel,
    pageLabel,
    restorePageFocusAfterDismissal,
    autoMount,
    mount,
  });
});
