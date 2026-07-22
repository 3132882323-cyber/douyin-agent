(async () => {
  const result = await chrome.runtime.sendMessage({ type: "start-full-scan", page_ids: ["overview", "orders", "live"] });
  document.getElementById("status").textContent = result?.ok ? "快速验证已启动。" : result?.error || "启动失败。";
  setTimeout(() => globalThis.close(), 1500);
})();
