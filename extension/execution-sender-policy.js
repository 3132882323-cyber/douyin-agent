(function exposeExecutionSenderPolicy(root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.DianExecutionSenderPolicy = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createExecutionSenderPolicy() {
  "use strict";

  const TRUSTED_EXECUTION_PAGES = Object.freeze(new Set(["/sidepanel.html"]));
  const TRUSTED_UI_PAGES = Object.freeze(new Set([
    "/sidepanel.html",
    "/popup.html",
    "/welcome.html",
    "/scan.html",
    "/smoke-scan.html",
    "/retry-scan.html",
    "/cancel-scan.html",
    "/sync.html",
  ]));
  const PLATFORM_HOSTS = Object.freeze({
    doudian: new Set(["fxg.jinritemai.com"]),
    qianchuan: new Set(["qianchuan.jinritemai.com", "buyin.jinritemai.com"]),
  });

  function expectedExtensionId(sender, extensionId) {
    const expectedId = String(extensionId || "").trim().toLowerCase();
    const senderId = String(sender?.id || "").trim().toLowerCase();
    return expectedId && senderId === expectedId ? expectedId : "";
  }

  function senderUrl(sender) {
    return String(sender?.url || sender?.tab?.url || "").trim();
  }

  function isTrustedExtensionPageSender(sender, extensionId, allowedPages = TRUSTED_UI_PAGES) {
    const expectedId = expectedExtensionId(sender, extensionId);
    if (!expectedId) return false;
    try {
      const parsed = new URL(senderUrl(sender));
      if (parsed.protocol !== "chrome-extension:" || parsed.hostname.toLowerCase() !== expectedId) return false;
      return allowedPages.has(parsed.pathname);
    } catch (_error) {
      return false;
    }
  }

  function isTrustedPageDataSender(sender, extensionId, source) {
    const expectedId = expectedExtensionId(sender, extensionId);
    const sourceName = String(source || "");
    const hosts = Object.prototype.hasOwnProperty.call(PLATFORM_HOSTS, sourceName)
      ? PLATFORM_HOSTS[sourceName]
      : null;
    if (!expectedId || !hosts || !Number.isInteger(Number(sender?.tab?.id))) return false;
    if (Number.isInteger(Number(sender?.frameId)) && Number(sender.frameId) !== 0) return false;
    try {
      const parsed = new URL(senderUrl(sender));
      return parsed.protocol === "https:" && hosts.has(parsed.hostname.toLowerCase());
    } catch (_error) {
      return false;
    }
  }

  function isTrustedExecutionSender(sender, extensionId) {
    return isTrustedExtensionPageSender(sender, extensionId, TRUSTED_EXECUTION_PAGES);
  }

  return Object.freeze({
    TRUSTED_EXECUTION_PAGES,
    TRUSTED_UI_PAGES,
    PLATFORM_HOSTS,
    isTrustedExtensionPageSender,
    isTrustedPageDataSender,
    isTrustedExecutionSender,
  });
});
