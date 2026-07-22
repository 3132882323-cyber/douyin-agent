(async () => {
  const status = document.getElementById("status");
  try {
    const result = await chrome.runtime.sendMessage({ type: "manual-sync" });
    const collected = (result.results || []).reduce((sum, item) => sum + (item.collected || 0), 0);
    status.textContent = `同步完成：${collected} 个页面。`;
  } catch (error) {
    status.textContent = `同步失败：${error.message || error}`;
  }
  setTimeout(() => globalThis.close(), 1500);
})();
