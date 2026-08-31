(function initDianBridgeAuth(root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  root.DianBridgeAuth = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function bridgeAuthFactory(root) {
  "use strict";

  const STORAGE_KEY = "dianAgentSessionV1";
  const AUTH_FAILURES = new Set([
    "agent_session_required",
    "agent_session_invalid",
    "agent_session_expired",
  ]);
  const DEFAULT_REFRESH_SKEW_MS = 30 * 1000;
  const DEFAULT_SESSION_TIMEOUT_MS = 2500;
  const EXTENSION_ID_HEADER = "X-Dian-Agent-Extension-Id";

  function object(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  }

  function expiryMs(value) {
    if (typeof value === "string" && value.trim() && !Number.isFinite(Number(value))) {
      const parsed = Date.parse(value);
      return Number.isFinite(parsed) ? parsed : 0;
    }
    const numeric = Number(value || 0);
    if (!Number.isFinite(numeric) || numeric <= 0) return 0;
    return numeric < 10_000_000_000 ? numeric * 1000 : numeric;
  }

  function isUsableSession(
    value,
    nowMs = Date.now(),
    skewMs = DEFAULT_REFRESH_SKEW_MS,
    expectedExtensionVersion = "",
  ) {
    const session = object(value);
    const expiresAt = expiryMs(session.expires_at);
    const expectedVersion = String(expectedExtensionVersion || "").trim();
    return Boolean(
      String(session.access_token || "").trim()
      && String(session.token_type || "DianAgent") === "DianAgent"
      && (!expectedVersion || String(session.extension_version || "").trim() === expectedVersion)
      && expiresAt > Number(nowMs) + Number(skewMs || 0),
    );
  }

  function authFailureCode(payload = {}) {
    const value = object(payload);
    const candidate = typeof value.error === "string"
      ? value.error
      : value.error_code || value.code || object(value.error).code;
    return String(candidate || "").trim().toLowerCase();
  }

  function isRefreshableAuthFailure(status, payload = {}) {
    return Number(status) === 401 && AUTH_FAILURES.has(authFailureCode(payload));
  }

  function browserFamily(userAgent = "") {
    const agent = String(userAgent || "");
    if (/Edg\//.test(agent)) return "edge";
    if (/QQBrowser\//.test(agent)) return "qq";
    if (/360(?:SE|EE)|QIHU/i.test(agent)) return "360";
    return "chrome";
  }

  function extensionSourcePayload(options = {}) {
    const manifest = object(options.manifest);
    const browser = browserFamily(options.userAgent);
    const storeSources = {
      chrome: "chrome_web_store",
      edge: "edge_addons",
      "360": "360_extension_store",
      qq: "chrome_web_store",
    };
    return {
      source: manifest.update_url ? storeSources[browser] : "unpacked",
      browser,
      version: String(manifest.version || ""),
      extension_id: String(options.extensionId || ""),
    };
  }

  function createHttpError(response, payload = {}) {
    const code = authFailureCode(payload);
    const message = typeof payload?.error === "string" && !AUTH_FAILURES.has(payload.error)
      ? payload.error
      : payload?.message || code || `本地 Agent 返回 HTTP ${response?.status || 0}`;
    const error = new Error(message);
    error.status = Number(response?.status || 0);
    error.code = code;
    error.payload = payload;
    return error;
  }

  function createClient(options = {}) {
    const baseUrl = String(options.baseUrl || "http://127.0.0.1:8765").replace(/\/$/, "");
    const fetchImpl = options.fetchImpl || root.fetch?.bind(root);
    const storage = options.storage === undefined ? root.chrome?.storage?.local : options.storage;
    const extensionId = String(options.extensionId || root.chrome?.runtime?.id || "").trim();
    const extensionVersion = String(
      options.extensionVersion || root.chrome?.runtime?.getManifest?.()?.version || "",
    ).trim();
    const now = typeof options.now === "function" ? options.now : () => Date.now();
    const refreshSkewMs = Number(options.refreshSkewMs ?? DEFAULT_REFRESH_SKEW_MS);
    const sessionTimeoutMs = Math.max(250, Number(options.sessionTimeoutMs ?? DEFAULT_SESSION_TIMEOUT_MS) || DEFAULT_SESSION_TIMEOUT_MS);
    if (typeof fetchImpl !== "function") throw new Error("DianBridgeAuth 需要 fetch 实现");

    let memorySession = null;
    let sessionPromise = null;
    let storageChecked = false;

    async function storageGet() {
      if (!storage?.get) return null;
      const result = await storage.get(STORAGE_KEY);
      return object(result)[STORAGE_KEY] || null;
    }

    async function storageSet(session) {
      if (storage?.set) await storage.set({ [STORAGE_KEY]: session });
    }

    async function storageRemove() {
      if (storage?.remove) await storage.remove(STORAGE_KEY);
    }

    async function clearSession() {
      memorySession = null;
      storageChecked = true;
      await storageRemove();
    }

    async function cachedSession() {
      if (isUsableSession(memorySession, now(), refreshSkewMs, extensionVersion)) return memorySession;
      memorySession = null;
      if (storageChecked) return null;
      storageChecked = true;
      const stored = await storageGet();
      if (isUsableSession(stored, now(), refreshSkewMs, extensionVersion)) {
        memorySession = stored;
        return stored;
      }
      if (stored) await storageRemove();
      return null;
    }

    async function issueSession() {
      if (!extensionVersion) {
        const error = new Error("Unable to determine the running extension version.");
        error.code = "agent_extension_version_required";
        throw error;
      }
      if (!extensionId) throw new Error("无法确认当前扩展 ID，拒绝连接本地 Agent");
      const controller = typeof root.AbortController === "function" ? new root.AbortController() : null;
      let timeoutId;
      const request = (async () => {
        const response = await fetchImpl(`${baseUrl}/auth/session`, {
          method: "POST",
          cache: "no-store",
          headers: {
            "Content-Type": "application/json",
            "X-Dian-Agent": "2",
            [EXTENSION_ID_HEADER]: extensionId,
            "X-Dian-Agent-Extension-Version": extensionVersion,
          },
          body: JSON.stringify({
            extension_id: extensionId,
            extension_version: extensionVersion,
          }),
          ...(controller ? { signal: controller.signal } : {}),
        });
        const payload = await response.json().catch(() => ({}));
        return { response, payload };
      })();
      const timeout = new Promise((_, reject) => {
        timeoutId = root.setTimeout(() => {
          controller?.abort();
          const error = new Error(`本地 Agent 会话建立超过 ${Math.ceil(sessionTimeoutMs / 1000)} 秒`);
          error.code = "agent_session_timeout";
          reject(error);
        }, sessionTimeoutMs);
      });
      let response;
      let payload;
      try {
        ({ response, payload } = await Promise.race([request, timeout]));
      } finally {
        root.clearTimeout(timeoutId);
      }
      if (!response.ok) throw createHttpError(response, payload);
      if (payload.ok !== true || !isUsableSession(payload, now(), 0, extensionVersion)) {
        throw new Error("本地 Agent 返回了无效会话，已拒绝继续请求");
      }
      memorySession = {
        ok: true,
        access_token: String(payload.access_token),
        token_type: "DianAgent",
        expires_in: Number(payload.expires_in || 0),
        expires_at: payload.expires_at,
        install_id: String(payload.install_id || ""),
        extension_version: extensionVersion,
      };
      storageChecked = true;
      await storageSet(memorySession);
      return memorySession;
    }

    async function getSession({ force = false } = {}) {
      if (!force) {
        const cached = await cachedSession();
        if (cached) return cached;
      }
      if (sessionPromise) return sessionPromise;
      sessionPromise = (async () => {
        if (force) await clearSession();
        return issueSession();
      })();
      try {
        return await sessionPromise;
      } finally {
        sessionPromise = null;
      }
    }

    async function responsePayload(response) {
      if (!response) return {};
      try {
        return await (typeof response.clone === "function" ? response.clone() : response).json();
      } catch (_) {
        return {};
      }
    }

    async function authorizedFetch(path, requestOptions = {}, internal = {}) {
      const session = await getSession({ force: internal.forceSession === true });
      const headers = {
        ...(requestOptions.headers || {}),
        "X-Dian-Agent-Token": session.access_token,
        [EXTENSION_ID_HEADER]: extensionId,
        "X-Dian-Agent-Extension-Version": extensionVersion,
      };
      const response = await fetchImpl(`${baseUrl}${path}`, {
        cache: "no-store",
        ...requestOptions,
        headers,
      });
      if (!internal.retried && Number(response.status) === 401) {
        const payload = await responsePayload(response);
        if (isRefreshableAuthFailure(response.status, payload)) {
          await clearSession();
          return authorizedFetch(path, requestOptions, { forceSession: true, retried: true });
        }
      }
      return response;
    }

    async function fetchJson(path, requestOptions = {}) {
      const response = await authorizedFetch(path, requestOptions);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw createHttpError(response, payload);
      return payload;
    }

    return {
      authorizedFetch,
      fetchJson,
      getSession,
      clearSession,
      session: () => memorySession,
    };
  }

  return {
    STORAGE_KEY,
    AUTH_FAILURES,
    DEFAULT_SESSION_TIMEOUT_MS,
    EXTENSION_ID_HEADER,
    expiryMs,
    isUsableSession,
    authFailureCode,
    isRefreshableAuthFailure,
    browserFamily,
    extensionSourcePayload,
    createClient,
  };
});
