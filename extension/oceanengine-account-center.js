(function initOceanEngineAccountCenter(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.DianOceanEngineAccountCenter = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createOceanEngineAccountCenter() {
  "use strict";

  const ACCOUNT_KEY = /^adacct_v1_[a-f0-9]{26}$/;
  const STATE_LABELS = Object.freeze({
    ready: "经营就绪",
    needs_auth: "需要授权",
    needs_sync: "等待同步",
    degraded: "需要处理",
    paused: "已暂停同步",
  });
  const ACTION_LABELS = Object.freeze({
    sync: "同步官方数据",
    schedule: "定时启停预演",
    pause: "暂停计划预演",
    budget_decrease: "降低预算预演",
  });

  function cleanText(value, limit) {
    return String(value || "").replace(/\s+/g, " ").trim().slice(0, limit);
  }

  function normalizeCapability(value = {}) {
    const allowedStates = new Set(["verified", "partial", "blocked", "untested"]);
    const state = allowedStates.has(value.state) ? value.state : "untested";
    return {
      id: cleanText(value.id, 32),
      label: cleanText(value.label, 24) || "未知能力",
      state,
      status_label: cleanText(value.status_label, 48) || "尚未验证",
      verified: state === "verified" && value.verified === true,
    };
  }

  function normalizeAccount(value = {}) {
    const key = String(value.account_key || "");
    if (!ACCOUNT_KEY.test(key)) return null;
    const state = Object.hasOwn(STATE_LABELS, value.state) ? value.state : "degraded";
    const preferences = value.preferences && typeof value.preferences === "object" ? value.preferences : {};
    const linkedStores = Array.isArray(value.linked_stores)
      ? value.linked_stores.slice(0, 20).map((store) => ({
        key: cleanText(store?.key, 48),
        label: cleanText(store?.label, 48) || "匿名店铺",
      }))
      : [];
    return {
      account_key: key,
      display_name: cleanText(value.display_name, 48) || `千川账户 ${key.slice(-6).toUpperCase()}`,
      platform_name: cleanText(value.platform_name, 48),
      masked_id: cleanText(value.masked_id, 24),
      role: cleanText(value.role, 32),
      selected: value.selected === true,
      valid: value.valid !== false,
      advertiser_count: Math.max(0, Number(value.advertiser_count || 0)),
      linked_stores: linkedStores,
      token: {
        state: cleanText(value.token?.state, 16) || "expired",
        label: cleanText(value.token?.label, 32) || "需要重新授权",
        expires_at: Number(value.token?.expires_at || 0) || null,
      },
      sync: {
        synced_at: Number(value.sync?.synced_at || 0) || null,
        freshness: {
          state: cleanText(value.sync?.freshness?.state, 16) || "never",
          label: cleanText(value.sync?.freshness?.label, 32) || "尚未同步",
        },
        endpoint_count: Math.max(0, Number(value.sync?.endpoint_count || 0)),
        success_count: Math.max(0, Number(value.sync?.success_count || 0)),
        failure_count: Math.max(0, Number(value.sync?.failure_count || 0)),
      },
      capabilities: Array.isArray(value.capabilities)
        ? value.capabilities.slice(0, 12).map(normalizeCapability).filter((item) => item.id)
        : [],
      preferences: {
        alias: cleanText(preferences.alias, 40),
        group_name: cleanText(preferences.group_name, 24) || "未分组",
        sync_enabled: preferences.sync_enabled !== false,
        managed: preferences.managed === true && preferences.sync_enabled !== false,
        revision: Math.max(0, Number(preferences.revision || 0)),
      },
      state,
      state_label: STATE_LABELS[state],
      next_action: cleanText(value.next_action, 64),
    };
  }

  function normalize(payload = {}) {
    const unsafe = payload.platform_write_enabled === true
      || payload.automatic_batch_submit === true
      || payload.secrets_exposed === true;
    const accounts = unsafe || !Array.isArray(payload.accounts)
      ? []
      : payload.accounts.map(normalizeAccount).filter(Boolean);
    const groups = [...new Set(accounts.map((item) => item.preferences.group_name))].sort((a, b) => a.localeCompare(b, "zh-CN"));
    return {
      schema_version: Number(payload.schema_version || 1),
      generated_at: Number(payload.generated_at || 0) || null,
      accounts,
      groups,
      summary: {
        total: accounts.length,
        connected: accounts.filter((item) => ["active", "expiring"].includes(item.token.state)).length,
        managed: accounts.filter((item) => item.preferences.managed).length,
        attention: accounts.filter((item) => !["ready", "paused"].includes(item.state)).length,
        advertisers: accounts.reduce((sum, item) => sum + item.advertiser_count, 0),
        groups: groups.length,
      },
      safe: !unsafe,
      platform_write_enabled: false,
      automatic_batch_submit: false,
      notice: unsafe
        ? "检测到账户接口返回了不安全的写入或密钥标记，账户中心已停止展示。"
        : cleanText(payload.notice, 160) || "账户批量投放动作只生成预演，不会自动提交。",
    };
  }

  function filterAccounts(accounts = [], filters = {}) {
    const query = cleanText(filters.query, 64).toLocaleLowerCase("zh-CN");
    const status = cleanText(filters.status, 24);
    const group = cleanText(filters.group, 24);
    return accounts.filter((account) => {
      const haystack = [
        account.display_name,
        account.platform_name,
        account.masked_id,
        account.preferences.group_name,
        ...account.linked_stores.map((store) => store.label),
      ].join(" ").toLocaleLowerCase("zh-CN");
      return (!query || haystack.includes(query))
        && (!status || status === "all" || account.state === status)
        && (!group || group === "all" || account.preferences.group_name === group);
    });
  }

  function preferencePayload(accountKey, value = {}) {
    if (!ACCOUNT_KEY.test(String(accountKey || ""))) throw new Error("账户身份无效，请刷新后重试");
    const syncEnabled = value.sync_enabled !== false;
    return {
      account_key: accountKey,
      alias: cleanText(value.alias, 40),
      group_name: cleanText(value.group_name, 24) || "未分组",
      sync_enabled: syncEnabled,
      managed: value.managed === true && syncEnabled,
    };
  }

  function selectedAccounts(accounts = [], selectedKeys = []) {
    const selected = new Set(selectedKeys);
    return accounts.filter((item) => selected.has(item.account_key));
  }

  function selectionSummary(accounts = [], selectedKeys = [], action = "sync") {
    const selected = selectedAccounts(accounts, selectedKeys);
    return {
      action: Object.hasOwn(ACTION_LABELS, action) ? action : "sync",
      action_label: ACTION_LABELS[action] || ACTION_LABELS.sync,
      account_count: selected.length,
      advertiser_count: selected.reduce((sum, item) => sum + item.advertiser_count, 0),
      managed_count: selected.filter((item) => item.preferences.managed).length,
      can_preview: selected.length > 0,
      platform_write_enabled: false,
    };
  }

  function formatTimestamp(timestamp) {
    const value = Number(timestamp || 0);
    if (!value) return "从未";
    return new Date(value * 1000).toLocaleString("zh-CN", { hour12: false });
  }

  return {
    ACTION_LABELS,
    STATE_LABELS,
    filterAccounts,
    formatTimestamp,
    normalize,
    preferencePayload,
    selectedAccounts,
    selectionSummary,
  };
});
