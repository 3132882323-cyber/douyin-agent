(async () => {
  const result = await chrome.runtime.sendMessage({ type: "retry-failed-scan" });
  document.getElementById("status").textContent = result?.ok ? `已启动 ${result.total} 个失败页面重试。` : result?.error || "无法启动重试。";
  setTimeout(() => globalThis.close(), 1500);
})();
