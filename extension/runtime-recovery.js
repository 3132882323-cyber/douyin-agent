/** Extension-page recovery shared by welcome, popup and the main workbench. */
(function exposeRuntimeRecovery(root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.DianRuntimeRecovery = api;
  if (root?.document && root?.chrome?.runtime?.id) api.install(root);
})(typeof globalThis !== "undefined" ? globalThis : this, function createRuntimeRecovery() {
  "use strict";

  const GUARD_PREFIX = "dianAgentRuntimeRecoveryV1:";
  const GUARD_WINDOW_MS = 20_000;
  const PROBE_INTERVAL_MS = 15_000;

  function isRecoverableRuntimeError(error) {
    return /extension context invalidated|receiving end does not exist|message port closed|could not establish connection/i.test(
      String(error?.message || error || ""),
    );
  }

  function guardKey(locationValue = {}) {
    return `${GUARD_PREFIX}${String(locationValue.pathname || "/")}`;
  }

  function readGuard(storage, key) {
    try {
      const value = JSON.parse(storage.getItem(key) || "null");
      return value && typeof value === "object" ? value : {};
    } catch (_) {
      return {};
    }
  }

  function canReload(storage, key, now = Date.now()) {
    const saved = readGuard(storage, key);
    return !(Number(saved.requested_at || 0) > 0 && now - Number(saved.requested_at) < GUARD_WINDOW_MS);
  }

  function rememberReload(storage, key, version, now = Date.now()) {
    try {
      storage.setItem(key, JSON.stringify({ requested_at: now, version: String(version || "") }));
    } catch (_) {
      // A blocked sessionStorage must not prevent the one-time page recovery.
    }
  }

  function clearGuard(storage, key) {
    try { storage.removeItem(key); } catch (_) { /* best effort */ }
  }

  function install(root) {
    if (root.__DianRuntimeRecoveryInstalled) return root.__DianRuntimeRecoveryInstalled;
    const runtime = root.chrome?.runtime;
    const storage = root.sessionStorage;
    const key = guardKey(root.location || {});
    let probing = false;
    let stopped = false;
    let intervalId = null;

    const notify = (name, detail = {}) => {
      try { root.dispatchEvent(new CustomEvent(name, { detail })); } catch (_) { /* optional UI signal */ }
    };

    const requestReload = (error) => {
      if (stopped || !isRecoverableRuntimeError(error)) return false;
      const now = Date.now();
      if (!canReload(storage, key, now)) {
        notify("dian-agent-runtime-recovery-blocked", { error: String(error?.message || error || "") });
        return false;
      }
      const version = (() => {
        try { return runtime.getManifest?.().version || ""; } catch (_) { return ""; }
      })();
      rememberReload(storage, key, version, now);
      stopped = true;
      if (intervalId) root.clearInterval(intervalId);
      notify("dian-agent-runtime-reloading", { error: String(error?.message || error || "") });
      root.setTimeout(() => root.location.reload(), 80);
      return true;
    };

    const probe = async () => {
      if (probing || stopped || root.document?.visibilityState === "hidden") return false;
      probing = true;
      try {
        const response = await runtime.sendMessage({ type: "runtime-context-ping" });
        if (!response?.ok) throw new Error(response?.error || "扩展后台未返回运行时回执");
        clearGuard(storage, key);
        notify("dian-agent-runtime-ready", response);
        return true;
      } catch (error) {
        requestReload(error);
        return false;
      } finally {
        probing = false;
      }
    };

    const onVisible = () => {
      if (root.document?.visibilityState !== "hidden") probe();
    };
    root.addEventListener("focus", probe);
    root.addEventListener("pageshow", probe);
    root.document?.addEventListener("visibilitychange", onVisible);
    root.setTimeout(probe, 250);
    intervalId = root.setInterval(probe, PROBE_INTERVAL_MS);

    const controller = Object.freeze({
      probe,
      recover: requestReload,
      dispose() {
        stopped = true;
        if (intervalId) root.clearInterval(intervalId);
        root.removeEventListener("focus", probe);
        root.removeEventListener("pageshow", probe);
        root.document?.removeEventListener("visibilitychange", onVisible);
      },
    });
    root.__DianRuntimeRecoveryInstalled = controller;
    return controller;
  }

  return Object.freeze({
    GUARD_PREFIX,
    GUARD_WINDOW_MS,
    PROBE_INTERVAL_MS,
    isRecoverableRuntimeError,
    guardKey,
    canReload,
    install,
  });
});
